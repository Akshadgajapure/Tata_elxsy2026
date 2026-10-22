"""Classifier evaluation module: confusion matrix, precision, recall, F1, unknown rate."""

from __future__ import annotations

from typing import Any
from .classifier import MetadataClassifier
from .models import FlowMetadata, TrafficClass


def _make_flow(**kwargs) -> FlowMetadata:
    defaults = dict(
        protocol="tcp",
        packet_rate=10.0,
        mean_packet_size=500.0,
        size_stddev=50.0,
        duration_seconds=5.0,
        bytes_per_second=500_000.0,
        device_id="10.0.0.1",
        dscp=0,
    )
    defaults.update(kwargs)
    return FlowMetadata(**defaults)


# Benchmark dataset with >=8 diverse test cases per class for strict evaluation
BENCHMARK_FLOWS: list[tuple[FlowMetadata, TrafficClass]] = [
    # REAL_TIME: VoIP, live calls, small packet UDP at regular rates or EF DSCP
    (_make_flow(protocol="udp", packet_rate=50, mean_packet_size=200, duration_seconds=10, bytes_per_second=10_000), TrafficClass.REAL_TIME),
    (_make_flow(protocol="udp", packet_rate=100, mean_packet_size=160, duration_seconds=30, bytes_per_second=16_000), TrafficClass.REAL_TIME),
    (_make_flow(protocol="udp", packet_rate=80, mean_packet_size=300, duration_seconds=60, bytes_per_second=24_000), TrafficClass.REAL_TIME),
    (_make_flow(protocol="udp", packet_rate=20, mean_packet_size=400, duration_seconds=5, bytes_per_second=8_000), TrafficClass.REAL_TIME),
    (_make_flow(protocol="udp", packet_rate=200, mean_packet_size=180, duration_seconds=12, bytes_per_second=36_000), TrafficClass.REAL_TIME),
    (_make_flow(protocol="udp", packet_rate=60, mean_packet_size=240, duration_seconds=90, bytes_per_second=14_400), TrafficClass.REAL_TIME),
    (_make_flow(protocol="udp", packet_rate=120, mean_packet_size=100, duration_seconds=15, bytes_per_second=12_000), TrafficClass.REAL_TIME),
    (_make_flow(protocol="udp", packet_rate=40, mean_packet_size=350, duration_seconds=8, bytes_per_second=14_000), TrafficClass.REAL_TIME),
    (_make_flow(protocol="udp", packet_rate=75, mean_packet_size=220, duration_seconds=4, bytes_per_second=16_500, dscp=46), TrafficClass.REAL_TIME),

    # INTERACTIVE: Gaming, low-latency UDP streams
    (_make_flow(protocol="udp", packet_rate=15, mean_packet_size=600, duration_seconds=2, bytes_per_second=9_000), TrafficClass.INTERACTIVE),
    (_make_flow(protocol="udp", packet_rate=30, mean_packet_size=800, duration_seconds=1, bytes_per_second=24_000), TrafficClass.INTERACTIVE),
    (_make_flow(protocol="udp", packet_rate=10, mean_packet_size=1000, duration_seconds=3, bytes_per_second=10_000), TrafficClass.INTERACTIVE),
    (_make_flow(protocol="udp", packet_rate=12, mean_packet_size=500, duration_seconds=4, bytes_per_second=6_000), TrafficClass.INTERACTIVE),
    (_make_flow(protocol="udp", packet_rate=280, mean_packet_size=600, duration_seconds=2, bytes_per_second=168_000), TrafficClass.INTERACTIVE),
    (_make_flow(protocol="udp", packet_rate=18, mean_packet_size=700, duration_seconds=1, bytes_per_second=12_600), TrafficClass.INTERACTIVE),
    (_make_flow(protocol="udp", packet_rate=8, mean_packet_size=1100, duration_seconds=2, bytes_per_second=8_800), TrafficClass.INTERACTIVE),
    (_make_flow(protocol="udp", packet_rate=16, mean_packet_size=500, duration_seconds=1, bytes_per_second=8_000), TrafficClass.INTERACTIVE),

    # ADAPTIVE_VIDEO: DASH / HLS video segments
    (_make_flow(protocol="tcp", packet_rate=50, duration_seconds=8, bytes_per_second=1_500_000, mean_packet_size=1400), TrafficClass.ADAPTIVE_VIDEO),
    (_make_flow(protocol="tcp", packet_rate=80, duration_seconds=9, bytes_per_second=1_800_000, mean_packet_size=1200), TrafficClass.ADAPTIVE_VIDEO),
    (_make_flow(protocol="tcp", packet_rate=60, duration_seconds=6, bytes_per_second=1_200_000, mean_packet_size=1300), TrafficClass.ADAPTIVE_VIDEO),
    (_make_flow(protocol="quic", packet_rate=70, duration_seconds=7, bytes_per_second=1_600_000, mean_packet_size=1000), TrafficClass.ADAPTIVE_VIDEO),
    (_make_flow(protocol="tcp", packet_rate=55, duration_seconds=8, bytes_per_second=900_000, mean_packet_size=1100), TrafficClass.ADAPTIVE_VIDEO),
    (_make_flow(protocol="tcp", packet_rate=65, duration_seconds=5, bytes_per_second=1_100_000, mean_packet_size=1350), TrafficClass.ADAPTIVE_VIDEO),
    (_make_flow(protocol="quic", packet_rate=90, duration_seconds=9, bytes_per_second=1_300_000, mean_packet_size=950), TrafficClass.ADAPTIVE_VIDEO),
    (_make_flow(protocol="tcp", packet_rate=45, duration_seconds=7, bytes_per_second=800_000, mean_packet_size=1200), TrafficClass.ADAPTIVE_VIDEO),

    # BULK: Long-lived high volume downloads
    (_make_flow(protocol="tcp", packet_rate=1000, duration_seconds=30, bytes_per_second=10_000_000, mean_packet_size=1460), TrafficClass.BULK),
    (_make_flow(protocol="tcp", packet_rate=800, duration_seconds=60, bytes_per_second=8_000_000, mean_packet_size=1460), TrafficClass.BULK),
    (_make_flow(protocol="tcp", packet_rate=1200, duration_seconds=20, bytes_per_second=12_000_000, mean_packet_size=1400), TrafficClass.BULK),
    (_make_flow(protocol="tcp", packet_rate=900, duration_seconds=45, bytes_per_second=9_000_000, mean_packet_size=1460), TrafficClass.BULK),
    (_make_flow(protocol="tcp", packet_rate=1500, duration_seconds=15, bytes_per_second=15_000_000, mean_packet_size=1400), TrafficClass.BULK),
    (_make_flow(protocol="tcp", packet_rate=700, duration_seconds=90, bytes_per_second=7_000_000, mean_packet_size=1460), TrafficClass.BULK),
    (_make_flow(protocol="quic", packet_rate=1100, duration_seconds=25, bytes_per_second=11_000_000, mean_packet_size=1380), TrafficClass.BULK),
    (_make_flow(protocol="tcp", packet_rate=600, duration_seconds=120, bytes_per_second=6_000_000, mean_packet_size=1460), TrafficClass.BULK),

    # BACKGROUND: Long-lived low-rate background updates
    (_make_flow(protocol="tcp", duration_seconds=120, bytes_per_second=100_000, packet_rate=10, mean_packet_size=1400), TrafficClass.BACKGROUND),
    (_make_flow(protocol="tcp", duration_seconds=300, bytes_per_second=200_000, packet_rate=15, mean_packet_size=1200), TrafficClass.BACKGROUND),
    (_make_flow(protocol="tcp", duration_seconds=180, bytes_per_second=50_000, packet_rate=5, mean_packet_size=1000), TrafficClass.BACKGROUND),
    (_make_flow(protocol="tcp", duration_seconds=600, bytes_per_second=300_000, packet_rate=20, mean_packet_size=1460), TrafficClass.BACKGROUND),
    (_make_flow(protocol="tcp", duration_seconds=240, bytes_per_second=150_000, packet_rate=12, mean_packet_size=1300), TrafficClass.BACKGROUND),
    (_make_flow(protocol="tcp", duration_seconds=90, bytes_per_second=80_000, packet_rate=8, mean_packet_size=1100), TrafficClass.BACKGROUND),
    (_make_flow(protocol="tcp", duration_seconds=400, bytes_per_second=400_000, packet_rate=25, mean_packet_size=1400), TrafficClass.BACKGROUND),
    (_make_flow(protocol="tcp", duration_seconds=200, bytes_per_second=250_000, packet_rate=18, mean_packet_size=1200), TrafficClass.BACKGROUND),

    # UNKNOWN: Short or ambiguous flows with insufficient metadata
    (_make_flow(protocol="tcp", duration_seconds=1.0, bytes_per_second=10_000, packet_rate=2, mean_packet_size=400), TrafficClass.UNKNOWN),
    (_make_flow(protocol="udp", duration_seconds=0.5, bytes_per_second=2_000, packet_rate=1, mean_packet_size=100), TrafficClass.UNKNOWN),
    (_make_flow(protocol="icmp", duration_seconds=0.2, bytes_per_second=100, packet_rate=1, mean_packet_size=64), TrafficClass.UNKNOWN),
    (_make_flow(protocol="tcp", duration_seconds=2.0, bytes_per_second=50_000, packet_rate=4, mean_packet_size=500), TrafficClass.UNKNOWN),
]


def evaluate_classifier() -> dict[str, Any]:
    """Runs MetadataClassifier on benchmark flows and computes full evaluation metrics."""
    clf = MetadataClassifier()
    classes = list(TrafficClass)

    conf_matrix: dict[TrafficClass, dict[TrafficClass, int]] = {
        a: {p: 0 for p in classes} for a in classes
    }
    predictions: list[tuple[TrafficClass, TrafficClass, float]] = []

    for flow, actual in BENCHMARK_FLOWS:
        result = clf.classify(flow)
        predicted = result.category
        conf_matrix[actual][predicted] += 1
        predictions.append((actual, predicted, result.confidence))

    per_class: dict[str, dict[str, float | int]] = {}
    total_correct = 0
    for cls in classes:
        tp = conf_matrix[cls][cls]
        fp = sum(conf_matrix[a][cls] for a in classes if a != cls)
        fn = sum(conf_matrix[cls][p] for p in classes if p != cls)
        tn = sum(conf_matrix[a][p] for a in classes for p in classes if a != cls and p != cls)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        per_class[cls.value] = {
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "support": tp + fn,
        }
        total_correct += tp

    total = len(BENCHMARK_FLOWS)
    accuracy = total_correct / total if total > 0 else 0.0
    unknown_count = sum(1 for _, pred, _ in predictions if pred == TrafficClass.UNKNOWN)
    unknown_rate = unknown_count / total if total > 0 else 0.0
    avg_confidence = sum(c for _, _, c in predictions) / total if total > 0 else 0.0

    macro_precision = sum(float(v["precision"]) for v in per_class.values()) / len(per_class)
    macro_recall = sum(float(v["recall"]) for v in per_class.values()) / len(per_class)
    macro_f1 = sum(float(v["f1"]) for v in per_class.values()) / len(per_class)

    return {
        "total_samples": total,
        "correct": total_correct,
        "accuracy": round(accuracy, 3),
        "macro_precision": round(macro_precision, 3),
        "macro_recall": round(macro_recall, 3),
        "macro_f1": round(macro_f1, 3),
        "unknown_rate": round(unknown_rate, 3),
        "avg_confidence": round(avg_confidence, 3),
        "per_class": per_class,
        "confusion_matrix": {
            a.value: {p.value: conf_matrix[a][p] for p in classes}
            for a in classes
        },
    }


def generate_confusion_matrix_text(results: dict[str, Any] | None = None) -> str:
    """Renders human-readable confusion matrix and performance statistics."""
    if results is None:
        results = evaluate_classifier()

    out = []
    out.append("================================================================================")
    out.append("             TATAELXSY TRAFFIC CLASSIFIER EVALUATION REPORT (PS3)              ")
    out.append("================================================================================")
    out.append(f"Total Evaluated Flows: {results['total_samples']} | Overall Accuracy: {results['accuracy']:.1%}")
    out.append(f"Macro Precision:       {results['macro_precision']:.1%} | Macro Recall:       {results['macro_recall']:.1%}")
    out.append(f"Macro F1 Score:        {results['macro_f1']:.1%} | Unknown Flow Rate:  {results['unknown_rate']:.1%}")
    out.append(f"Mean Confidence Score: {results['avg_confidence']:.2f}")
    out.append("--------------------------------------------------------------------------------")
    out.append("Per-Class Metrics:")
    for cls_name, metrics in results["per_class"].items():
        if metrics["support"] > 0:
            out.append(f"  {cls_name:16s} -> Precision: {metrics['precision']:.2f} | Recall: {metrics['recall']:.2f} | F1: {metrics['f1']:.2f} (n={metrics['support']})")

    out.append("--------------------------------------------------------------------------------")
    out.append("Confusion Matrix (Rows: Ground Truth, Columns: Predicted Class):")
    classes = [c.value for c in TrafficClass]
    header = f"{'Actual \\ Pred':18s}" + "".join(f"{c[:7]:>9s}" for c in classes)
    out.append(header)
    for actual in classes:
        row = results["confusion_matrix"].get(actual, {})
        line = f"{actual:18s}" + "".join(f"{row.get(p, 0):9d}" for p in classes)
        out.append(line)
    out.append("================================================================================")
    return "\n".join(out)
