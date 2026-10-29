"""Explicit local bootstrap and review; credentials never come from arguments or environment."""

import ipaddress
import json
from functools import wraps
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from .errors import DomainError
from .fixtures import baseline_dataset, demo_datasets
from .schemas import BaselineRequest, EvaluateRequest, ReviewRequest
from .service import SPCService
from .storage import Store, new_id

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False,
                  help="Local SPC with original synthetic data; explicit human baseline review.")


def _output(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _safe(command):
    @wraps(command)
    def wrapped(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except DomainError as exc:
            typer.echo(f"{exc.code}: {exc.message}", err=True)
            raise typer.Exit(1) from None
        except ValidationError:
            typer.echo("validation_error: Invalid fields, types or ranges", err=True)
            raise typer.Exit(2) from None
        except (typer.Exit, typer.Abort):
            raise
        except Exception:
            typer.echo("local_error: Local operation failed; internal details withheld", err=True)
            raise typer.Exit(1) from None
    return wrapped


def _safe_path(data: Path) -> Path:
    path = data.expanduser().absolute()
    if ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise DomainError("unsafe_path", "Symlinks and parent traversal are not allowed", 409)
    return path


def _existing_store(data: Path) -> Store:
    path = _safe_path(data)
    if not path.is_dir() or not (path / "spc.sqlite3").is_file():
        raise DomainError("not_initialized", "Initialize a new data directory first", 409)
    return Store(path)


def _authenticated(data: Path):
    store = _existing_store(data)
    token = typer.prompt("Bearer token (hidden)", hide_input=True)
    if not token or token.strip() != token or len(token) > 200:
        raise DomainError("unauthorized", "Authentication required", 401)
    return SPCService(store), store.authenticate(token)


@app.command("init")
@_safe
def initialize(data: Path):
    """Create a NEW directory, import original fixtures and display one admin token once."""
    path = _safe_path(data)
    # Existing empty directories are deliberately refused too; never rotate a prior identity.
    if path.exists():
        raise DomainError("init_exists", "Init requires a new, nonexistent directory", 409)
    if not path.parent.is_dir():
        raise DomainError("init_parent", "The parent directory must already exist", 409)
    path.mkdir(mode=0o700, exist_ok=False)
    store = Store(path)
    baseline = baseline_dataset()
    token = store.provision_user("local-admin", "admin", [baseline.plant_id])
    actor = store.authenticate(token)
    service = SPCService(store)
    datasets = [baseline, *demo_datasets().values()]
    for dataset in datasets:
        service.import_dataset(dataset.model_dump(mode="json"), actor)
    _output({"actor_id": actor.actor_id, "role": actor.role, "plants": actor.plants,
             "token": token, "datasets_imported": len(datasets),
             "notice": "Token shown once; only its hash is stored. Original synthetic data only. "
                       "No baseline was built or approved."})


@app.command("fixture")
@_safe
def fixture(case: Annotated[str, typer.Option("--case")] = "baseline"):
    """Print an original Dataset JSON object; baseline is the default case."""
    if case == "baseline":
        dataset = baseline_dataset()
    else:
        dataset = demo_datasets().get(case)
    if dataset is None:
        raise DomainError("unknown_case", "Unknown original synthetic fixture case")
    _output(dataset)


@app.command("build")
@_safe
def build(data: Path, dataset_id: str):
    """Build a frozen DRAFT baseline. Authentication uses a hidden prompt."""
    service, actor = _authenticated(data)
    _output(service.create_baseline(BaselineRequest(
        action_id=new_id("build"), dataset_id=dataset_id), actor))


@app.command("approve")
@_safe
def approve(data: Path, baseline_id: str,
            content_hash: Annotated[str, typer.Argument(metavar="HASH")],
            action_id: str, note: str):
    """Explicitly approve an exact baseline hash with an idempotent human review note."""
    request = ReviewRequest(action_id=action_id, content_hash=content_hash,
                            decision="approved", note=note)
    service, actor = _authenticated(data)
    _output(service.review_baseline(baseline_id, request, actor))


@app.command("evaluate")
@_safe
def evaluate(data: Path, baseline_id: str, dataset_id: str):
    """Evaluate measurements only against an explicitly approved frozen baseline."""
    service, actor = _authenticated(data)
    _output(service.evaluate(EvaluateRequest(action_id=new_id("evaluate"),
                                            dataset_id=dataset_id, baseline_id=baseline_id), actor))


@app.command("serve")
@_safe
def serve(
    data: Path,
    runtime_config: Annotated[Path | None, typer.Option("--runtime-config")] = None,
    model_url: Annotated[str | None, typer.Option("--model-url")] = None,
    test_mode: Annotated[bool, typer.Option("--test-mode")] = False,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8004,
):
    """Choose exactly one explicit runtime; never fall back or read sensitive environment config."""
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local or not 1 <= port <= 65535:
        raise DomainError("listen_address", "Serve requires a numeric loopback IP and port 1..65535")
    if sum((runtime_config is not None, model_url is not None, test_mode)) != 1:
        raise DomainError("provider_choice", "Choose exactly one of --runtime-config, --model-url, --test-mode")
    if runtime_config is not None and not runtime_config.is_absolute():
        raise DomainError("runtime_config", "Runtime configuration must be an absolute path")
    from .api import create_app, provider_metadata
    from .provider import LocalProvider, SyntheticProvider

    if runtime_config is not None:
        from .managed_provider import ManagedLocalProvider
        provider = ManagedLocalProvider(runtime_config)
    elif model_url is not None:
        provider = LocalProvider(model_url)
    else:
        provider = SyntheticProvider()
    store = _existing_store(data)
    application = create_app(store.root, provider)
    typer.echo(json.dumps(provider_metadata(provider), ensure_ascii=False), err=True)
    import uvicorn
    uvicorn.run(application, host=host, port=port, access_log=False,
                proxy_headers=False, server_header=False, log_level="warning")


def main(argv=None):
    app(args=argv, prog_name="spc-analysis")


if __name__ == "__main__":
    main()
