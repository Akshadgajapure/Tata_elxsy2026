"""Flask + Flask-SocketIO control console for the adaptive QoS prototype.

Run on Windows (simulation/demo mode):
    python app.py

Run on Linux for namespace / tc operation with appropriate privileges:
    sudo -E env "PATH=$PATH" python app.py
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict

from flask import Flask, Response, jsonify, render_template, request
from flask_socketio import SocketIO

from qos_controller import config as cfg
from qos_controller.experiments.report import (
    generate_csv_report,
    generate_html_report,
    generate_json_report,
    generate_markdown_report,
)
from qos_controller.experiments.runner import ExperimentRunner
from qos_controller.hotspot_monitor import HotspotMonitor
from qos_controller.intent import INTENT_PROFILES
from qos_controller.models import TrafficClass
from qos_controller.netns import CommandError
from qos_controller.runtime import Runtime
from qos_controller.telemetry import (NetnsCounterSource, NullProbe, NullQdiscSource,
                                       RttProbe, SimulatedCounterSource, TcQdiscSource)
from qos_controller.traffic import PROFILES, TrafficGenerator
from windows_capture.companion import WindowsCaptureCompanion

log = logging.getLogger(__name__)

PAGES = [
    ("MONITOR", [("overview", "Overview", "/"), ("traffic", "Live Traffic", "/traffic"),
                 ("clients", "Clients", "/clients"), ("hotspot", "Hotspot Monitor", "/hotspot"),
                 ("congestion", "Congestion", "/congestion")]),
    ("CONTROL", [("policies", "QoS Policies", "/policies"), ("intent", "Intent Control", "/intent"),
                 ("rollback", "Rollback", "/rollback"), ("safety", "Safety", "/safety")]),
    ("ANALYSIS", [("experiments", "Experiments", "/experiments"), ("decisions", "Decision Log", "/decisions")]),
    ("SYSTEM", [("controller", "Controller", "/controller"), ("telemetry", "Telemetry", "/telemetry"),
                ("tc", "Linux TC", "/tc"), ("architecture", "Architecture Spec (PDF)", "/architecture")]),
]

PLANNED_EXPERIMENTS = [
    ("iso_video_call", "ISO download + video call"),
    ("capacity_drop", "Capacity drop 100 -> 20 Mbit/s"),
    ("streams_gaming", "Multiple streams + gaming"),
    ("work_call_intent", "Temporary work-call intent"),
    ("misclassification", "Misclassification / correction"),
    ("enforcement_failure", "Enforcement failure + rollback"),
]


def create_app(runtime: Runtime | None = None,
               generator: TrafficGenerator | None = None,
               autostart: bool = True,
               hotspot_monitor: HotspotMonitor | None = None,
               experiment_runner: ExperimentRunner | None = None) -> tuple[Flask, SocketIO, Runtime]:
    app = Flask(
        __name__,
        template_folder=str(os.path.join(os.path.dirname(__file__), "web", "templates")),
        static_folder=str(os.path.join(os.path.dirname(__file__), "web", "static")),
    )
    socketio = SocketIO(app, async_mode="threading")
    emit = lambda event, payload: socketio.emit(event, payload)  # noqa: E731

    if runtime is None:
        generator = generator or TrafficGenerator()
        _is_windows = os.name == "nt"
        if _is_windows:
            # Windows: use synthetic counter source driven by iperf profiles and no-op probe.
            log.info("Windows detected — using SimulatedCounterSource(generator) + NullProbe")
            probe = NullProbe()
            counters = SimulatedCounterSource(generator)
            qdisc = NullQdiscSource()
        else:
            probe = RttProbe()
            counters = NetnsCounterSource()
            qdisc = TcQdiscSource(cfg.ROUTER_SERVER_IFACE)

        runtime = Runtime(counters, qdisc, probe, generator, emit=emit)
        if autostart:
            probe.start()
            runtime.start()
    else:
        runtime.emit = emit
        generator = runtime.generator

    hotspot = hotspot_monitor or HotspotMonitor()
    runner = experiment_runner or ExperimentRunner(output_dir=cfg.EXPERIMENT_OUTPUT_DIR)
    companion = WindowsCaptureCompanion(output_dir=cfg.HOTSPOT_CAPTURE_DIR)

    def page(template: str, active: str):
        return render_template(template, pages=PAGES, active=active,
                                profiles=list(PROFILES.values()), clients=cfg.CLIENTS,
                                planned=PLANNED_EXPERIMENTS, intent_profiles=INTENT_PROFILES,
                                traffic_classes=[c.value for c in TrafficClass])

    @app.get("/")
    def overview(): return page("overview.html", "overview")

    @app.get("/traffic")
    def traffic_page(): return page("traffic.html", "traffic")

    @app.get("/clients")
    def clients_page(): return page("clients.html", "clients")

    @app.get("/hotspot")
    def hotspot_page(): return page("hotspot.html", "hotspot")

    @app.get("/policies")
    def policies_page(): return page("policies.html", "policies")

    @app.get("/decisions")
    def decisions_page(): return page("decisions.html", "decisions")

    @app.get("/intent")
    def intent_page(): return page("intent.html", "intent")

    @app.get("/congestion")
    def congestion_page(): return page("congestion.html", "congestion")

    @app.get("/safety")
    def safety_page(): return page("safety.html", "safety")

    @app.get("/rollback")
    def rollback_page(): return page("rollback.html", "rollback")

    @app.get("/experiments")
    def experiments_page(): return page("experiments.html", "experiments")

    @app.get("/controller")
    def controller_page(): return page("controller.html", "controller")

    @app.get("/telemetry")
    def telemetry_page(): return page("telemetry.html", "telemetry")

    @app.get("/tc")
    def tc_page(): return page("tc.html", "tc")

    @app.get("/architecture")
    def architecture_page():
        from scripts.generate_architecture_doc import build_architecture_html
        return Response(build_architecture_html(), mimetype="text/html")

    @app.get("/api/status")
    def api_status():
        latest = runtime.latest or {}
        return jsonify({"telemetry_ok": latest.get("telemetry_ok"),
                        "error": latest.get("error"),
                        "enforcement": runtime._enforcement(),
                        "root": os.geteuid() == 0 if hasattr(os, "geteuid") else None,
                        "tick_seconds": cfg.TICK_SECONDS})

    @app.get("/api/metrics")
    def api_metrics(): return jsonify(runtime.snapshot())

    @app.get("/api/clients")
    def api_clients(): return jsonify((runtime.latest or {}).get("clients", []))

    @app.get("/api/policy")
    def api_policy(): return jsonify(runtime.policy_view())

    @app.get("/api/safety")
    def api_safety(): return jsonify((runtime.latest or {}).get("safety"))

    @app.get("/api/safety/detail")
    def api_safety_detail(): return jsonify(runtime.safety_view())

    @app.get("/api/rollback")
    def api_rollback(): return jsonify(runtime.rollback_view())

    @app.get("/api/congestion")
    def api_congestion(): return jsonify(runtime.congestion_view())

    @app.get("/api/intent")
    def api_intent(): return jsonify(runtime.intent_view())

    @app.post("/api/intent")
    def api_intent_activate():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "JSON body required"}), 400
        try:
            view = runtime.activate_intent(body.get("intent"), body.get("target"),
                                           body.get("duration_seconds"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(view)

    @app.post("/api/intent/cancel")
    def api_intent_cancel():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "JSON body required"}), 400
        try:
            cancelled = runtime.cancel_intent(body.get("target"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"cancelled": cancelled, **runtime.intent_view()})

    # Stage A compatibility endpoint
    @app.get("/api/experiments")
    def api_experiments():
        return jsonify({"runner_implemented": False,
                        "message": "Experiment runner not yet implemented",
                        "planned": [{"id": i, "name": n, "status": "NOT IMPLEMENTED"}
                                    for i, n in PLANNED_EXPERIMENTS]})

    # Experiment Runner endpoints (Priority 4)
    @app.get("/api/experiments/scenarios")
    def api_exp_scenarios():
        return jsonify(runner.list_scenarios())

    @app.post("/api/experiments/run")
    def api_exp_run():
        body = request.get_json(silent=True) or {}
        scenario_id = body.get("scenario_id")
        if not scenario_id:
            return jsonify({"error": "scenario_id is required"}), 400
        try:
            job_id = runner.start_scenario(scenario_id)
            return jsonify({"job_id": job_id, "scenario_id": scenario_id, "status": "queued"})
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/experiments/jobs")
    def api_exp_jobs():
        return jsonify(runner.list_jobs())

    @app.get("/api/experiments/job/<job_id>")
    def api_exp_job(job_id: str):
        job = runner.get_job(job_id)
        if not job:
            return jsonify({"error": f"Job {job_id} not found"}), 404
        return jsonify({
            "job_id": job.job_id,
            "scenario_id": job.scenario_id,
            "scenario_name": job.scenario_name,
            "state": job.state,
            "progress": job.progress,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "result": asdict(job.result) if job.result else None,
            "error": job.error,
        })

    @app.get("/api/experiments/report/<job_id>/<fmt>")
    def api_exp_report(job_id: str, fmt: str):
        job = runner.get_job(job_id)
        if not job or not job.result:
            return jsonify({"error": f"Result for job {job_id} not available"}), 404
        data = asdict(job.result)
        fmt = fmt.lower()
        if fmt == "html":
            return Response(generate_html_report(data), mimetype="text/html")
        elif fmt in ("md", "markdown"):
            return Response(generate_markdown_report(data), mimetype="text/markdown")
        elif fmt == "csv":
            return Response(generate_csv_report(data), mimetype="text/csv")
        elif fmt == "json":
            return Response(generate_json_report(data), mimetype="application/json")
        return jsonify({"error": f"Unsupported format: {fmt}. Use html, md, csv, or json"}), 400

    # Hotspot / Connected Devices endpoints (Synthetic iperf data)
    @app.get("/api/hotspot/status")
    def api_hotspot_status():
        st = asdict(companion.get_status())
        st["continuous"] = companion.is_continuous
        return jsonify({
            "summary": hotspot.get_summary(),
            "capture_status": st,
        })

    @app.get("/api/hotspot/devices")
    def api_hotspot_devices():
        if getattr(hotspot, "_synthetic_demo_enabled", False):
            hotspot.tick_synthetic_from_iperf()
        return jsonify(hotspot.get_devices())

    @app.post("/api/hotspot/synthetic")
    def api_hotspot_synthetic():
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled", False))
        res = hotspot.set_synthetic_demo(enabled)
        return jsonify({"ok": True, "enabled": res, "devices": hotspot.get_devices()})

    @app.post("/api/hotspot/label")
    def api_hotspot_label():
        body = request.get_json(silent=True) or {}
        ip, label = body.get("ip"), body.get("label")
        if not ip or not label:
            return jsonify({"error": "Both 'ip' and 'label' are required"}), 400
        try:
            hotspot.set_device_label(ip, label)
            return jsonify({"ok": True, "ip": ip, "label": label})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/hotspot/classify")
    def api_hotspot_classify():
        body = request.get_json(silent=True) or {}
        ip, cls_name = body.get("ip"), body.get("traffic_class")
        if not ip or not cls_name:
            return jsonify({"error": "Both 'ip' and 'traffic_class' are required"}), 400
        try:
            hotspot.set_device_class_override(ip, cls_name)
            return jsonify({"ok": True, "ip": ip, "traffic_class": cls_name})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/hotspot/ingest")
    def api_hotspot_ingest():
        """Accepts JSON array of packet metadata (src_ip, dst_ip, protocol, length, timestamp)."""
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "JSON object with 'records' array required"}), 400
        records = body.get("records")
        if not isinstance(records, list):
            return jsonify({"error": "'records' must be an array"}), 400
        # Bound record count to prevent memory exhaustion
        if len(records) > 50000:
            return jsonify({"error": "Batch limit exceeded (maximum 50,000 records per call)"}), 400

        source_kind = body.get("source_kind", "imported_pcapng")
        res = hotspot.ingest_packet_records(records, source_kind=source_kind)
        return jsonify(res)

    @app.post("/api/hotspot/capture")
    def api_hotspot_capture():
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        comp_id = body.get("component_id")
        try:
            if action == "start":
                st = companion.start_capture(component_id=comp_id)
                return jsonify(st)
            elif action == "stop":
                st = companion.stop_capture()
                return jsonify(st)
            else:
                return jsonify({"error": "action must be 'start' or 'stop'"}), 400
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/hotspot/continuous")
    def api_hotspot_continuous():
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        if action == "start":
            interval = int(body.get("interval", 5))
            ok = companion.start_continuous_loop(
                interval_seconds=interval,
                callback=lambda recs: hotspot.ingest_packet_records(recs, source_kind="real_capture")
            )
            return jsonify({"ok": ok, "continuous": True, "message": f"Continuous auto-capture active ({interval}s bursts)."})
        elif action == "stop":
            companion.stop_continuous_loop()
            return jsonify({"ok": True, "continuous": False, "message": "Continuous auto-capture stopped."})
        return jsonify({"error": "action must be 'start' or 'stop'"}), 400

    # Backend selection & status (Priority 1)
    @app.get("/api/backend")
    def api_backend():
        return jsonify({
            "configured_backend": cfg.ENFORCEMENT_BACKEND,
            "active_backend": runtime._backend_name,
            "tc_namespace": cfg.TC_NAMESPACE,
            "tc_iface": cfg.TC_IFACE,
            "shaping_direction": "egress (client->server)",
            "details": runtime._enforcement(),
        })

    @app.get("/api/system/controller")
    def api_sys_controller(): return jsonify(runtime.controller_view())

    @app.get("/api/system/telemetry")
    def api_sys_telemetry(): return jsonify(runtime.telemetry_view())

    @app.get("/api/system/tc")
    def api_sys_tc(): return jsonify(runtime.tc_view())

    @app.get("/api/decisions")
    def api_decisions(): return jsonify(runtime.decision_list())

    @app.get("/api/traffic")
    def api_traffic_status(): return jsonify(generator.status())

    @app.post("/api/traffic")
    def api_traffic():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "JSON body required"}), 400
        name, action = body.get("profile"), body.get("action")
        if name not in PROFILES or action not in {"start", "stop"}:
            return jsonify({"error": "profile/action invalid"}), 400
        try:
            if action == "start":
                duration = body.get("duration", 120)
                if isinstance(duration, bool) or not isinstance(duration, int):
                    return jsonify({"error": "duration must be an integer"}), 400
                info = generator.start(name, duration)
                runtime.log_event("TRAFFIC_STARTED", f"Traffic profile '{name}' started",
                                  profile=name, duration_s=duration,
                                  description=PROFILES[name].description)
            else:
                stopped = generator.stop(name)
                info = generator.status_one(name)
                if stopped:
                    runtime.log_event("TRAFFIC_STOPPED", f"Traffic profile '{name}' stopped",
                                      profile=name)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except CommandError as exc:
            return jsonify({"error": f"could not run traffic: {exc}"}), 500
        return jsonify(info)

    @socketio.on("connect")
    def on_connect():
        socketio.emit("history", runtime.snapshot()["history"], to=request.sid)
        socketio.emit("decisions", runtime.decision_list(), to=request.sid)

    return app, socketio, runtime


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        log.warning("Not running as root: entering network namespaces will fail.")
    app, socketio, runtime = create_app()
    host = os.environ.get("QOS_WEB_HOST", "127.0.0.1")
    try:
        socketio.run(app, host=host, port=int(os.environ.get("QOS_WEB_PORT", "5000")),
                     allow_unsafe_werkzeug=True)
    finally:
        runtime.stop()
        runtime.generator.shutdown()
        runtime.probe.stop()


if __name__ == "__main__":
    main()
