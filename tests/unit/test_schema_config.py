"""Unit tests for the declarative schema loader/builder (krossdb.schema)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text

from krossdb.core.exceptions import ConfigurationError
from krossdb.schema import SchemaConfig, build_metadata, load_schema_config
from krossdb.schema.config import ColumnSpec, TableSpec

pytestmark = pytest.mark.unit

VALID_CONFIG: dict = {
    "tables": [
        {
            "name": "organizations",
            "columns": [
                {"name": "id", "type": "uuid", "primary_key": True},
                {"name": "name", "type": "string", "length": 120, "required": True, "unique": True},
            ],
        },
        {
            "name": "users",
            "columns": [
                {"name": "id", "type": "uuid", "primary_key": True},
                {"name": "org_id", "type": "uuid", "required": True},
                {"name": "email", "type": "string", "required": True},
                {"name": "created_at", "type": "datetime", "required": True, "default": "now"},
            ],
            "foreign_keys": [
                {
                    "columns": ["org_id"],
                    "ref_table": "organizations",
                    "ref_columns": ["id"],
                    "on_delete": "CASCADE",
                }
            ],
            "indexes": [{"columns": ["email"], "unique": True}],
        },
    ]
}


def test_table_without_primary_key_is_rejected():
    with pytest.raises(ValidationError, match="no primary_key column"):
        TableSpec.model_validate(
            {"name": "widgets", "columns": [{"name": "name", "type": "string"}]}
        )


def test_foreign_key_referencing_undefined_column_is_rejected():
    with pytest.raises(ValidationError, match="undefined column"):
        TableSpec.model_validate(
            {
                "name": "users",
                "columns": [{"name": "id", "type": "uuid", "primary_key": True}],
                "foreign_keys": [
                    {"columns": ["org_id"], "ref_table": "organizations", "ref_columns": ["id"]}
                ],
            }
        )


def test_foreign_key_referencing_unknown_table_is_rejected():
    with pytest.raises(ValidationError, match="unknown table"):
        SchemaConfig.model_validate(
            {
                "tables": [
                    {
                        "name": "users",
                        "columns": [
                            {"name": "id", "type": "uuid", "primary_key": True},
                            {"name": "org_id", "type": "uuid"},
                        ],
                        "foreign_keys": [
                            {
                                "columns": ["org_id"],
                                "ref_table": "does_not_exist",
                                "ref_columns": ["id"],
                            }
                        ],
                    }
                ]
            }
        )


def test_foreign_key_column_count_mismatch_is_rejected():
    with pytest.raises(ValidationError, match="1 column\\(s\\) but references 2"):
        TableSpec.model_validate(
            {
                "name": "users",
                "columns": [
                    {"name": "id", "type": "uuid", "primary_key": True},
                    {"name": "org_id", "type": "uuid"},
                ],
                "foreign_keys": [
                    {
                        "columns": ["org_id"],
                        "ref_table": "organizations",
                        "ref_columns": ["id", "tenant"],
                    }
                ],
            }
        )


def test_load_schema_config_missing_file_raises_configuration_error(tmp_path):
    with pytest.raises(ConfigurationError, match="not found"):
        load_schema_config(tmp_path / "does-not-exist.yaml")


def test_load_schema_config_malformed_yaml_raises_configuration_error(tmp_path):
    path = tmp_path / "schema.yaml"
    path.write_text("tables: [this is not: valid: yaml:", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="failed to parse"):
        load_schema_config(path)


def test_load_schema_config_invalid_schema_raises_configuration_error(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"tables": []}), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="invalid schema config"):
        load_schema_config(path)


def test_load_schema_config_reads_yaml(tmp_path):
    path = tmp_path / "schema.yaml"
    path.write_text(
        "tables:\n"
        "  - name: widgets\n"
        "    columns:\n"
        "      - name: id\n"
        "        type: uuid\n"
        "        primary_key: true\n",
        encoding="utf-8",
    )

    config = load_schema_config(path)

    assert [t.name for t in config.tables] == ["widgets"]


def test_load_schema_config_reads_json(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")

    config = load_schema_config(path)

    assert {t.name for t in config.tables} == {"organizations", "users"}


def test_build_metadata_produces_correct_ddl_shape():
    config = SchemaConfig.model_validate(VALID_CONFIG)

    metadata = build_metadata(config)

    organizations = metadata.tables["organizations"]
    assert organizations.c.name.unique is True

    users = metadata.tables["users"]
    assert {c.name for c in users.primary_key.columns} == {"id"}
    assert users.c.email.nullable is False
    assert users.c.org_id.nullable is False
    assert len(users.foreign_keys) == 1
    fk = next(iter(users.foreign_keys))
    assert fk.column.table.name == "organizations"
    assert fk.ondelete == "CASCADE"
    assert any(idx.unique for idx in users.indexes)


def test_build_metadata_creates_real_tables_on_sqlite():
    config = SchemaConfig.model_validate(VALID_CONFIG)
    metadata = build_metadata(config)
    engine = create_engine("sqlite:///:memory:")

    try:
        metadata.create_all(engine)

        table_names = set(inspect(engine).get_table_names())
        assert {"organizations", "users"} <= table_names

        with engine.connect() as conn:
            conn.execute(
                text("INSERT INTO organizations (id, name) VALUES ('org-1', 'Acme')")
            )
            conn.execute(
                text(
                    "INSERT INTO users (id, org_id, email, created_at) "
                    "VALUES ('u-1', 'org-1', 'a@example.com', '2026-01-01 00:00:00')"
                )
            )
            conn.commit()
            rows = conn.execute(text("SELECT email FROM users")).fetchall()

        assert rows == [("a@example.com",)]
    finally:
        engine.dispose()


def test_build_metadata_extends_an_existing_metadata_object():
    from sqlalchemy import Column, Integer, MetaData, Table

    existing = MetaData()
    Table("legacy", existing, Column("id", Integer, primary_key=True))
    config = SchemaConfig.model_validate(VALID_CONFIG)

    result = build_metadata(config, metadata=existing)

    assert result is existing
    assert {"legacy", "organizations", "users"} <= set(result.tables.keys())


@pytest.mark.parametrize(
    ("column_type", "sqlalchemy_type_name"),
    [
        ("string", "String"),
        ("text", "Text"),
        ("integer", "Integer"),
        ("bigint", "BigInteger"),
        ("float", "Float"),
        ("numeric", "Numeric"),
        ("boolean", "Boolean"),
        ("date", "Date"),
        ("datetime", "DateTime"),
        ("uuid", "Uuid"),
        ("json", "JSON"),
    ],
)
def test_each_column_type_maps_to_a_sqlalchemy_type(column_type, sqlalchemy_type_name):
    spec = ColumnSpec.model_validate({"name": "col", "type": column_type, "primary_key": True})
    table_spec = TableSpec.model_validate(
        {"name": "t", "columns": [spec.model_dump(mode="json")]}
    )
    metadata = build_metadata(SchemaConfig.model_validate({"tables": [table_spec.model_dump(mode="json")]}))

    assert type(metadata.tables["t"].c.col.type).__name__ == sqlalchemy_type_name


def test_datetime_column_type_is_always_timezone_aware():
    table_spec = {
        "name": "t",
        "columns": [
            {"name": "id", "type": "uuid", "primary_key": True},
            {"name": "at", "type": "datetime"},
        ],
    }
    metadata = build_metadata(SchemaConfig.model_validate({"tables": [table_spec]}))

    assert metadata.tables["t"].c.at.type.timezone is True
