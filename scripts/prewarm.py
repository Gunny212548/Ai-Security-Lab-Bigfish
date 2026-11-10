"""Pre-warm + smoke test: send one tiny request to every provider/model the active challenges use.

    python scripts/prewarm.py            # only what challenges/*.yaml use
    python scripts/prewarm.py --all      # every provider in config/providers.yaml
    docker compose exec app python scripts/prewarm.py

Run it ~15 minutes before the workshop. It loads the local model into memory (the first Ollama request
can take a minute) and proves the cloud API key, model name and network path work. Exit code 1 if any fail.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.challenges import load_challenges  # noqa: E402
from app.llm import _clean, get_client  # noqa: E402  (_clean: strip <think>/<thought> like the app does)
from app.settings import Provider, Settings, get_settings, load_providers  # noqa: E402

PING = [{"role": "user", "content": "Reply with the single word OK."}]


def targets(settings: Settings, providers: dict[str, Provider], every: bool) -> dict[tuple[str, str], list[str]]:
    """(provider, model) -> challenge ids that use it."""
    out: dict[tuple[str, str], list[str]] = {}
    if every:
        for name, p in providers.items():
            out.setdefault((name, p.model), [])
    for ch in load_challenges(settings.challenges_dir, set(providers)).values():
        name = ch.provider or settings.default_provider
        out.setdefault((name, ch.model or providers[name].model), []).append(ch.id)
    return out


async def ping(provider: Provider, model: str, settings: Settings) -> tuple[bool, float, str]:
    kwargs = {"model": model, "messages": PING, "max_tokens": provider.max_tokens or settings.llm_max_tokens}
    start = time.perf_counter()
    try:
        resp = await get_client(provider, settings).chat.completions.create(**kwargs)
    except Exception as e:  # noqa: BLE001 - report every failure the same way
        return False, time.perf_counter() - start, f"{type(e).__name__}: {str(e)[:160]}"
    text = _clean(resp.choices[0].message.content).replace("\n", " ")
    return True, time.perf_counter() - start, text[:60] or "(empty reply)"


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--all", action="store_true", help="ping every provider in providers.yaml, not only the used ones")
    args = ap.parse_args(argv)

    settings = get_settings()
    providers = load_providers(settings.providers_file, settings.default_provider)
    failed = 0
    print(f"{'provider':<12} {'model':<28} {'used by':<16} {'result':<6} {'time':>6}  reply")
    for (name, model), cids in targets(settings, providers, args.all).items():
        p = providers[name]
        if p.api_key_env and not p.resolve_api_key(settings):
            ok, secs, detail = False, 0.0, f"{p.api_key_env} is not set in .env"
        else:
            ok, secs, detail = await ping(p, model, settings)
        failed += not ok
        print(f"{name:<12} {model:<28} {','.join(cids) or '-':<16} {'OK' if ok else 'FAIL':<6} {secs:>5.1f}s  {detail}")
    print("\nAll providers answered." if not failed else f"\n{failed} provider(s) failed. Fix before the workshop.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
