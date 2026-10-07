"""TokenSQL command-line entry points.

Every heavyweight or optional component is imported inside the command that needs
it. This keeps help and profiling usable when API, UI, or persistence modules are
not installed yet.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
import importlib
import importlib.util
import inspect
import json
import re
from pathlib import Path
import subprocess
import sys
from typing import Any, Optional

import typer


app = typer.Typer(
    name="tokensql",
    help="Traceable TokenSQL database, import, official refresh, API, and UI commands.",
    no_args_is_help=True,
)


class CliRuntimeError(RuntimeError):
    """An expected, user-actionable CLI failure."""


def _abort(message: str) -> None:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=2)


def _load_module(module_name: str, purpose: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        raise CliRuntimeError(
            f"{purpose} is unavailable because {module_name!r} could not be loaded "
            f"({type(exc).__name__}: {exc})."
        ) from exc


def _build_profile(header_row: int, discount_scale: Optional[str]) -> Any:
    importer = _load_module("tokensql.importers", "Excel profiling")
    scale: Optional[Decimal] = None
    if discount_scale is not None:
        try:
            scale = Decimal(discount_scale)
        except InvalidOperation as exc:
            raise CliRuntimeError("--discount-scale must be a decimal value.") from exc
    try:
        return importer.ParserProfile(
            header_row=header_row,
            discount_scale=scale,
        )
    except (TypeError, ValueError) as exc:
        raise CliRuntimeError(f"Invalid parser profile: {exc}") from exc


def _load_profile_file(path: Path) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CliRuntimeError(f"Cannot read parser profile {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CliRuntimeError(
            f"Parser profile {path} is not valid JSON: {exc.msg} at line {exc.lineno}."
        ) from exc

    if not isinstance(payload, dict):
        raise CliRuntimeError("Parser profile JSON must be an object.")
    allowed = {"header_row", "discount_scale", "price_headers"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise CliRuntimeError(
            "Parser profile has unsupported keys: " + ", ".join(unknown)
        )
    if "price_headers" in payload:
        headers = payload["price_headers"]
        if not isinstance(headers, list) or not all(
            isinstance(item, str) and item.strip() for item in headers
        ):
            raise CliRuntimeError("profile.price_headers must be a list of strings.")
        payload["price_headers"] = tuple(headers)
    if payload.get("discount_scale") is not None:
        try:
            payload["discount_scale"] = Decimal(str(payload["discount_scale"]))
        except InvalidOperation as exc:
            raise CliRuntimeError(
                "profile.discount_scale must be a decimal value."
            ) from exc

    importer = _load_module("tokensql.importers", "Excel profiling")
    try:
        return importer.ParserProfile(**payload)
    except (TypeError, ValueError) as exc:
        raise CliRuntimeError(f"Invalid parser profile: {exc}") from exc


def _profile_payload(result: Any) -> dict[str, Any]:
    counts = Counter(
        getattr(item.parsed_price.kind, "value", str(item.parsed_price.kind))
        for item in result.staged_quotes
    )
    return {
        "source_name": result.workbook.source_name,
        "sha256": result.workbook.sha256,
        "byte_size": result.workbook.byte_size,
        "needs_review": result.workbook.needs_review,
        "review_reasons": list(result.workbook.review_reasons),
        "staged_quote_count": len(result.staged_quotes),
        "staged_quote_review_count": sum(
            bool(item.needs_review) for item in result.staged_quotes
        ),
        "price_kind_counts": dict(sorted(counts.items())),
        "sheets": [
            {
                "title": sheet.title,
                "max_row": sheet.max_row,
                "max_column": sheet.max_column,
                "header_row": sheet.header_row,
                "data_row_count": sheet.data_row_count,
                "merge_count": len(sheet.merge_ranges),
                "needs_review": sheet.needs_review,
                "review_reasons": list(sheet.review_reasons),
            }
            for sheet in result.workbook.sheets
        ],
    }


def _profile_workbook(path: Path, profile: Any) -> Any:
    importer = _load_module("tokensql.importers", "Excel profiling")
    try:
        return importer.parse_excel(path, profile)
    except Exception as exc:
        raise CliRuntimeError(
            f"Workbook profiling failed ({type(exc).__name__}: {exc})."
        ) from exc


def _resolve_alembic_paths(
    explicit_config: Optional[Path] = None,
) -> tuple[Path, Path]:
    """Find an Alembic config and sibling migration directory safely."""
    if explicit_config is not None:
        candidates = [explicit_config.expanduser().resolve()]
    else:
        candidates = [
            (Path.cwd() / "alembic.ini").resolve(),
            Path(__file__).resolve().parents[2] / "alembic.ini",
        ]

    checked: list[Path] = []
    for config_path in candidates:
        if config_path in checked:
            continue
        checked.append(config_path)
        migrations_path = config_path.parent / "alembic"
        if config_path.is_file() and migrations_path.is_dir():
            return config_path, migrations_path

    locations = ", ".join(str(path) for path in checked)
    raise CliRuntimeError(
        "alembic.ini and its sibling alembic migration directory were not "
        f"found; checked: {locations}."
    )


def _upgrade_database(
    database_url: Optional[str],
    revision: str,
    alembic_config_path: Optional[Path] = None,
) -> None:
    alembic_command = _load_module("alembic.command", "Database migrations")
    alembic_config = _load_module("alembic.config", "Database migrations")
    settings_module = _load_module("tokensql.config", "TokenSQL configuration")

    config_path, migrations_path = _resolve_alembic_paths(alembic_config_path)
    url = database_url or settings_module.get_settings().database_url
    config = alembic_config.Config(str(config_path))
    config.set_main_option("script_location", str(migrations_path))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    try:
        alembic_command.upgrade(config, revision)
    except Exception as exc:
        raise CliRuntimeError(
            f"Database upgrade failed ({type(exc).__name__}: {exc})."
        ) from exc


def _import_workbook(
    path: Path,
    source_id: int,
    provider_id: int,
    profile: Any,
    parser_version: str,
    database_url: Optional[str],
) -> Any:
    service_module = _load_module(
        "tokensql.services.import_service", "Excel persistence service"
    )
    session_module = _load_module("tokensql.db.session", "Database session")
    try:
        context = service_module.ImportContext(
            data_source_id=source_id,
            provider_id=provider_id,
            parser_version=parser_version,
            profile=profile,
        )
        service = service_module.ExcelImportService()
        engine = session_module.create_db_engine(database_url)
        factory = session_module.create_session_factory(engine)
        try:
            with session_module.session_scope(factory) as session:
                return service.import_file(session, path, context)
        finally:
            engine.dispose()
    except Exception as exc:
        raise CliRuntimeError(
            f"Excel import failed ({type(exc).__name__}: {exc})."
        ) from exc


def _bootstrap_catalog(path: Path, database_url: Optional[str]) -> Any:
    service_module = _load_module(
        "tokensql.services.bootstrap_service", "Catalog bootstrap service"
    )
    session_module = _load_module("tokensql.db.session", "Database session")
    try:
        engine = session_module.create_db_engine(database_url)
        try:
            factory = session_module.create_session_factory(engine)
            with session_module.session_scope(factory) as session:
                return service_module.BootstrapService(session).apply_file(path)
        finally:
            engine.dispose()
    except Exception as exc:
        raise CliRuntimeError(
            f"Catalog bootstrap failed ({type(exc).__name__}: {exc})."
        ) from exc


_IMPORT_PATH = re.compile(
    r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$"
)


def _load_official_adapter(import_path: str) -> Any:
    """Load an adapter object or no-argument factory from module:attribute."""
    value = import_path.strip()
    if not _IMPORT_PATH.fullmatch(value):
        raise CliRuntimeError(
            "--adapter must use a safe Python import path: module:attribute."
        )
    module_name, attribute_path = value.split(":", 1)
    module = _load_module(module_name, "Official price adapter")
    target: Any = module
    try:
        for part in attribute_path.split("."):
            target = getattr(target, part)
    except AttributeError as exc:
        raise CliRuntimeError(
            f"Official price adapter attribute {value!r} does not exist."
        ) from exc

    service_module = _load_module(
        "tokensql.services.official_service", "Official price service"
    )
    protocol = service_module.OfficialPriceAdapter
    candidate = target
    try:
        target_is_adapter = isinstance(target, protocol)
    except Exception as exc:
        raise CliRuntimeError(
            f"Official price adapter {value!r} could not be validated."
        ) from exc
    if inspect.isclass(target) or not target_is_adapter:
        if not callable(target):
            raise CliRuntimeError(
                f"Official price adapter {value!r} is neither an adapter nor a factory."
            )
        try:
            inspect.signature(target).bind()
        except (TypeError, ValueError) as exc:
            raise CliRuntimeError(
                f"Official price adapter factory {value!r} must accept no arguments."
            ) from exc
        try:
            candidate = target()
        except Exception as exc:
            raise CliRuntimeError(
                f"Official price adapter factory {value!r} failed "
                f"({type(exc).__name__})."
            ) from exc

    try:
        candidate_is_adapter = isinstance(candidate, protocol)
    except Exception as exc:
        raise CliRuntimeError(
            f"Official price adapter {value!r} could not be validated."
        ) from exc
    if not candidate_is_adapter:
        raise CliRuntimeError(
            f"Official price adapter {value!r} does not implement "
            "OfficialPriceAdapter (source_name, supports, url_for, parse)."
        )
    try:
        source_name = candidate.source_name
    except Exception as exc:
        raise CliRuntimeError(
            f"Official price adapter {value!r} has an unreadable source_name."
        ) from exc
    if not isinstance(source_name, str) or not source_name.strip():
        raise CliRuntimeError(
            f"Official price adapter {value!r} must expose a non-empty source_name."
        )
    if not all(
        callable(getattr(candidate, name, None))
        for name in ("supports", "url_for", "parse")
    ):
        raise CliRuntimeError(
            f"Official price adapter {value!r} has a non-callable protocol method."
        )
    return candidate


def _official_domains(values: list[str]) -> tuple[str, ...]:
    domains: list[str] = []
    for value in values:
        domain = value.strip().rstrip(".").casefold()
        try:
            ascii_domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            ascii_domain = ""
        if (
            not ascii_domain
            or len(ascii_domain) > 253
            or "://" in ascii_domain
            or any(token in ascii_domain for token in ("/", "@", ":", "*"))
            or any(character.isspace() for character in ascii_domain)
            or not re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                ascii_domain,
            )
        ):
            raise CliRuntimeError(
                "--allowed-domain values must be exact hostnames, not URLs, "
                "wildcards, ports, or credentials."
            )
        domains.append(ascii_domain)
    if not domains:
        raise CliRuntimeError("At least one --allowed-domain is required.")
    return tuple(dict.fromkeys(domains))


def _official_summary(results: Any) -> tuple[dict[str, Any], bool]:
    items = [
        {
            "offering_id": item.offering_id,
            "status": item.status,
            "source_artifact_id": item.source_artifact_id,
            "import_batch_id": item.import_batch_id,
            "candidate_count": item.candidate_count,
            "reused_existing": item.reused_existing,
        }
        for item in results
    ]
    failed = any(item["status"] in {"failed", "skipped"} for item in items)
    return (
        {
            "status": "partial_failure" if failed else "completed",
            "offering_count": len(items),
            "failed_count": sum(
                item["status"] in {"failed", "skipped"} for item in items
            ),
            "results": items,
        },
        failed,
    )


def _run_official_refresh(
    *,
    data_source_id: int,
    allowed_domains: list[str],
    adapter_path: str,
    database_url: str,
    limit: Optional[int],
    timeout: float,
) -> tuple[dict[str, Any], bool]:
    """Execute exactly one official refresh transaction and close all resources."""
    domains = _official_domains(allowed_domains)
    adapter = _load_official_adapter(adapter_path)
    session_module = _load_module("tokensql.db.session", "Database session")
    official_module = _load_module(
        "tokensql.services.official_service", "Official price service"
    )
    http_module = _load_module("tokensql.official.http", "Official HTTP client")

    engine = None
    client = None
    try:
        engine = session_module.create_db_engine(database_url)
        factory = session_module.create_session_factory(engine)
        client = http_module.OfficialHttpClient(
            allowed_domains=domains,
            timeout=timeout,
        )
        with session_module.session_scope(factory) as session:
            service = official_module.OfficialPriceService(
                session,
                data_source_id=data_source_id,
                client=client,
                adapters=(adapter,),
            )
            results = service.run_once(limit=limit)
        return _official_summary(results)
    except CliRuntimeError:
        raise
    except Exception as exc:
        # Do not echo exception messages: driver errors can embed database URLs or keys.
        raise CliRuntimeError(
            f"Official refresh failed safely ({type(exc).__name__}); "
            "inspect restricted application logs and review records."
        ) from exc
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if engine is not None:
            engine.dispose()


def _run_api(host: str, port: int, reload: bool) -> None:
    api_module = _load_module("tokensql.api.app", "FastAPI application")
    api_app = getattr(api_module, "app", None)
    if api_app is None:
        raise CliRuntimeError("tokensql.api.app does not export an 'app' object.")
    uvicorn = _load_module("uvicorn", "Uvicorn")
    try:
        target = "tokensql.api.app:app" if reload else api_app
        uvicorn.run(target, host=host, port=port, reload=reload)
    except Exception as exc:
        raise CliRuntimeError(
            f"API server failed ({type(exc).__name__}: {exc})."
        ) from exc


def _run_ui(host: str, port: int) -> None:
    try:
        spec = importlib.util.find_spec("tokensql.ui.app")
    except (ImportError, ModuleNotFoundError, AttributeError) as exc:
        raise CliRuntimeError(
            "Streamlit application 'tokensql.ui.app' is unavailable."
        ) from exc
    if spec is None or spec.origin is None:
        raise CliRuntimeError(
            "Streamlit application 'tokensql.ui.app' is unavailable."
        )
    if importlib.util.find_spec("streamlit") is None:
        raise CliRuntimeError(
            "Streamlit is not installed; install the 'ui' optional dependency."
        )
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        spec.origin,
        "--server.address",
        host,
        "--server.port",
        str(port),
    ]
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CliRuntimeError(
            f"Streamlit exited unsuccessfully ({type(exc).__name__}: {exc})."
        ) from exc


@app.command("db-upgrade")
def db_upgrade(
    database_url: Optional[str] = typer.Option(
        None,
        "--database-url",
        envvar="TOKENSQL_DATABASE_URL",
        help="SQLAlchemy URL. Defaults to TOKENSQL_DATABASE_URL/application settings.",
    ),
    alembic_config_path: Optional[Path] = typer.Option(
        None,
        "--alembic-config",
        envvar="TOKENSQL_ALEMBIC_CONFIG",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="alembic.ini path; defaults to the current directory, then source tree.",
    ),
    revision: str = typer.Option("head", help="Alembic revision to upgrade to."),
) -> None:
    """Apply Alembic migrations to an explicitly selected database."""

    try:
        _upgrade_database(database_url, revision, alembic_config_path)
    except CliRuntimeError as exc:
        _abort(str(exc))
    typer.echo(f"Database upgraded to {revision}.")


@app.command("bootstrap-catalog")
def bootstrap_catalog(
    file: Path = typer.Option(
        ...,
        "--file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Strict JSON catalog document to apply atomically.",
    ),
    database_url: Optional[str] = typer.Option(
        None,
        "--database-url",
        envvar="TOKENSQL_DATABASE_URL",
        help="SQLAlchemy URL. Defaults to TOKENSQL_DATABASE_URL/application settings.",
    ),
) -> None:
    """Idempotently seed source, model, provider, plan, and metric catalog data."""

    try:
        result = _bootstrap_catalog(file, database_url)
    except CliRuntimeError as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))


@app.command("profile-xlsx")
def profile_xlsx(
    path: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Workbook to inspect without database writes.",
    ),
    header_row: int = typer.Option(1, min=1, help="One-based header row."),
    discount_scale: Optional[str] = typer.Option(
        None,
        help="Explicit discount scale; only 10 is currently supported.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit a machine-readable JSON summary."
    ),
) -> None:
    """Inspect an XLSX workbook; this command never writes to the database."""

    try:
        profile = _build_profile(header_row, discount_scale)
        result = _profile_workbook(path, profile)
    except CliRuntimeError as exc:
        _abort(str(exc))

    payload = _profile_payload(result)
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    typer.echo(f"Workbook: {payload['source_name']}")
    typer.echo(f"SHA-256: {payload['sha256']}")
    typer.echo(
        "Sheets: "
        f"{len(payload['sheets'])}; staged quotes: {payload['staged_quote_count']}; "
        f"requiring review: {payload['staged_quote_review_count']}"
    )
    for sheet in payload["sheets"]:
        typer.echo(
            f"- {sheet['title']}: {sheet['data_row_count']} data rows, "
            f"{sheet['merge_count']} merged ranges, review={sheet['needs_review']}"
        )


@app.command("import-xlsx")
def import_xlsx(
    path: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    source_id: int = typer.Option(
        ...,
        "--source-id",
        min=1,
        help="Existing data_source primary key; the parser never invents it.",
    ),
    provider_id: int = typer.Option(
        ..., "--provider-id", min=1, help="Existing reviewed provider ID."
    ),
    profile_path: Path = typer.Option(
        ...,
        "--profile",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Required JSON parser profile reviewed for this workbook.",
    ),
    parser_version: str = typer.Option(
        "cli-v1", help="Parser version recorded for auditability."
    ),
    database_url: Optional[str] = typer.Option(
        None,
        "--database-url",
        envvar="TOKENSQL_DATABASE_URL",
        help="SQLAlchemy URL. Defaults to TOKENSQL_DATABASE_URL/application settings.",
    ),
) -> None:
    """Import an XLSX using an existing source ID and an explicit JSON profile."""

    try:
        profile = _load_profile_file(profile_path)
        result = _import_workbook(
            path=path,
            source_id=source_id,
            provider_id=provider_id,
            profile=profile,
            parser_version=parser_version,
            database_url=database_url,
        )
    except CliRuntimeError as exc:
        _abort(str(exc))
    typer.echo(f"Excel import completed: {result!s}")


@app.command("official-refresh")
def official_refresh(
    data_source_id: int = typer.Option(
        ...,
        "--data-source-id",
        min=1,
        help="Primary official data_source ID; IDs are never guessed.",
    ),
    allowed_domain: list[str] = typer.Option(
        ...,
        "--allowed-domain",
        help="Exact official HTTPS hostname to allow; repeat for multiple hosts.",
    ),
    adapter: str = typer.Option(
        ...,
        "--adapter",
        help="Reviewed OfficialPriceAdapter object/factory as module:attribute.",
    ),
    database_url: str = typer.Option(
        ...,
        "--database-url",
        envvar="TOKENSQL_DATABASE_URL",
        help="Required SQLAlchemy database URL (or TOKENSQL_DATABASE_URL).",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        min=1,
        help="Maximum number of missing-price offerings to attempt once.",
    ),
    timeout: float = typer.Option(
        10.0,
        "--timeout",
        min=0.1,
        max=120.0,
        help="Per-request timeout in seconds.",
    ),
) -> None:
    """Run one explicit official-price refresh; never starts a background loop."""
    try:
        payload, failed = _run_official_refresh(
            data_source_id=data_source_id,
            allowed_domains=allowed_domain,
            adapter_path=adapter,
            database_url=database_url,
            limit=limit,
            timeout=timeout,
        )
    except CliRuntimeError as exc:
        _abort(str(exc))
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if failed:
        raise typer.Exit(code=1)


@app.command("serve-api")
def serve_api(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8000, min=1, max=65535, help="Bind port."),
    reload: bool = typer.Option(False, help="Enable Uvicorn development reload."),
) -> None:
    """Run the FastAPI application."""

    try:
        _run_api(host, port, reload)
    except CliRuntimeError as exc:
        _abort(str(exc))


@app.command("run-ui")
def run_ui(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8501, min=1, max=65535, help="Bind port."),
) -> None:
    """Run the optional Streamlit management UI."""

    try:
        _run_ui(host, port)
    except CliRuntimeError as exc:
        _abort(str(exc))


if __name__ == "__main__":
    app()
