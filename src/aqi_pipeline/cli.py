"""Command line for the AQI pipeline.

    aqi-pipeline simulate                      full demo on synthetic data, with report
    aqi-pipeline bootstrap --until 2026-01-01  load history from the configured source, train v001
    aqi-pipeline run-cycle                     one cycle up to now (call from cron or a scheduler)
    aqi-pipeline schedule --every 3600         run cycles in a loop
    aqi-pipeline status                        champion, latest signals and decision
    aqi-pipeline report                        rebuild the HTML monitoring report
    aqi-pipeline rollback                      manually restore the shadow model
    aqi-pipeline serve                         start the HTTP API
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import pandas as pd

from .config import load_config
from .pipeline import Pipeline
from .report import build_report
from .sources import make_source


def _pipeline(args, with_source: bool = True) -> Pipeline:
    cfg = load_config(args.config, {"run_dir": args.run_dir} if args.run_dir else None)
    source = make_source(cfg.source) if with_source and cfg.source.get("type") != "synthetic" else None
    return Pipeline(cfg, source)


def _now(args) -> pd.Timestamp:
    return pd.Timestamp(args.now) if getattr(args, "now", None) else pd.Timestamp.now("UTC").tz_localize(None).floor("h")


def cmd_simulate(args) -> int:
    from .simulate import run_simulation
    overrides = {"run_dir": args.run_dir} if args.run_dir else {}
    cfg = load_config(args.config, overrides)
    if args.days:
        from .simulate import simulation_settings
        sim = simulation_settings(cfg)
        end = pd.Timestamp(sim["production_start"]) + pd.Timedelta(days=args.days)
        cfg.simulation = {**cfg.simulation, "production_end": str(end.date())}
    summary = run_simulation(cfg, verbose=not args.quiet)
    path = build_report(Pipeline(cfg), summary)
    print(f"\nreport:  {path}\nsummary: {cfg.run_dir}/summary.json")
    return 0


def cmd_bootstrap(args) -> int:
    pipe = _pipeline(args)
    if pipe.source is None:
        print("configure a csv or open_meteo source to bootstrap (or use `simulate`)", file=sys.stderr)
        return 2
    version = pipe.bootstrap(pd.Timestamp(args.until) if args.until else _now(args))
    meta = pipe.registry.meta(version)
    print(f"{version}: {meta['kind']} trained on {meta['n_train_rows']} rows, "
          f"backtest MAE {meta['validation']['mae']:.1f} ({meta['validation']['rel_mae']:.2f} x persistence)")
    return 0


def cmd_run_cycle(args) -> int:
    pipe = _pipeline(args)
    record = pipe.run_cycle(_now(args))
    d = record["decision"]
    print(f"{d['action']}: {d['reason']}")
    if "retrain" in record:
        r = record["retrain"]
        print(("promoted " + r.get("version", "")) if r["promoted"] else "challenger rejected", "-", r["reason"])
    return 0


def cmd_schedule(args) -> int:
    pipe = _pipeline(args)
    while True:
        started = time.time()
        try:
            record = pipe.run_cycle(pd.Timestamp.now("UTC").tz_localize(None).floor("h"))
            print(f"{pd.Timestamp.now():%Y-%m-%d %H:%M} {record['decision']['action']}: {record['decision']['reason']}",
                  flush=True)
        except Exception as exc:          # keep the loop alive; the next cycle retries
            print(f"cycle failed: {exc!r}", file=sys.stderr, flush=True)
        time.sleep(max(1.0, args.every - (time.time() - started)))


def cmd_status(args) -> int:
    pipe = _pipeline(args, with_source=False)
    champion = pipe.registry.champion
    if not champion:
        print("no model yet; run bootstrap or simulate")
        return 1
    meta = pipe.registry.meta(champion)
    last = pipe.store.latest_cycle() or {}
    out = {
        "champion": champion, "shadow": pipe.registry.shadow, "trained_at": meta["trained_at"],
        "model": meta["kind"], "strategy": meta["strategy"], "trigger": meta["trigger"],
        "last_cycle": last.get("cycle_time"), "decision": last.get("decision", {}).get("action"),
        "reason": last.get("decision", {}).get("reason"), "safe_mode": last.get("safe_mode"),
        "performance": last.get("performance"), "drift_level": last.get("drift", {}).get("level"),
        "drift_score": last.get("drift", {}).get("score"), "drifted_inputs": last.get("drift", {}).get("drifted"),
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_report(args) -> int:
    pipe = _pipeline(args, with_source=False)
    summary = None
    summary_path = pipe.cfg.db_path.parent / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    print(build_report(pipe, summary, args.out))
    return 0


def cmd_rollback(args) -> int:
    pipe = _pipeline(args, with_source=False)
    now = _now(args)
    restored = pipe.registry.rollback(now, args.reason)
    pipe.store.log_event(now, "rollback", "warning", f"manual rollback to {restored}: {args.reason}")
    print(f"champion is now {restored}")
    return 0


def cmd_serve(args) -> int:
    import os

    import uvicorn
    if args.config:
        os.environ["AQI_CONFIG"] = args.config
    if args.run_dir:
        os.environ["AQI_RUN_DIR"] = args.run_dir
    uvicorn.run("aqi_pipeline.api:app", host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aqi-pipeline", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="YAML config file")
    parser.add_argument("--run-dir", help="where the database, registry and reports live")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("simulate", help="run the synthetic end-to-end demo")
    p.add_argument("--days", type=int, help="shorten the production period to this many days")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("bootstrap", help="load history and train the first model")
    p.add_argument("--until", help="use history up to this time (default: now)")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("run-cycle", help="run one pipeline cycle")
    p.add_argument("--now", help="cycle time (default: current hour, UTC)")
    p.set_defaults(func=cmd_run_cycle)

    p = sub.add_parser("schedule", help="run cycles forever")
    p.add_argument("--every", type=float, default=3600.0, help="seconds between cycles")
    p.set_defaults(func=cmd_schedule)

    sub.add_parser("status", help="show champion and latest decision").set_defaults(func=cmd_status)

    p = sub.add_parser("report", help="build the HTML monitoring report")
    p.add_argument("--out", help="output path")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("rollback", help="restore the shadow model as champion")
    p.add_argument("--reason", default="manual rollback")
    p.add_argument("--now")
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("serve", help="start the HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
