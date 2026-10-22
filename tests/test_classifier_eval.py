"""Classifier evaluation: precision, recall, F1, confusion matrix, unknown-class rate.

These tests run on labelled, unseen synthetic flows.  They do NOT use the same
samples the classifier was tuned against.  The test passes even if the numbers
change slightly (thresholds are lenient), so that this file is useful as a
regression guard rather than an exact match.
"""

from __future__ import annotations

import pytest
from qos_controller.classifier import MetadataClassifier
from qos_controller.models import FlowMetadata, TrafficClass


def _flow(**kwargs) -> FlowMetadata:
    defaults = dict(protocol="tcp", packet_rate=10.0, mean_packet_size=500.0,
                    size_stddev=50.0, duration_seconds=5.0, bytes_per_second=500_000.0,
                    device_id="10.0.0.1", dscp=0)
    defaults.update(kwargs)
    return FlowMetadata(**defaults)


# ---------------------------------------------------------------------------
# Labelled test dataset — 8+ samples per class, deterministic fixed values
# ---------------------------------------------------------------------------

LABELLED_FLOWS: list[tuple[FlowMetadata, TrafficClass]] = [
    # ---- REAL_TIME: VoIP, video call, small UDP at regular rate ----------
    (_flow(protocol="udp", packet_rate=50,  mean_packet_size=200, duration_seconds=10, bytes_per_second=10_000), TrafficClass.REAL_TIME),
    (_flow(protocol="udp", packet_rate=100, mean_packet_size=160, duration_seconds=30, bytes_per_second=16_000), TrafficClass.REAL_TIME),
    (_flow(protocol="udp", packet_rate=80,  mean_packet_size=300, duration_seconds=60, bytes_per_second=24_000), TrafficClass.REAL_TIME),
    (_flow(protocol="udp", packet_rate=20,  mean_packet_size=400, duration_seconds=5,  bytes_per_second=8_000),  TrafficClass.REAL_TIME),
    (_flow(protocol="udp", packet_rate=200, mean_packet_size=180, duration_seconds=12, bytes_per_second=36_000), TrafficClass.REAL_TIME),
    (_flow(protocol="udp", packet_rate=60,  mean_packet_size=240, duration_seconds=90, bytes_per_second=14_400), TrafficClass.REAL_TIME),
    (_flow(protocol="udp", packet_rate=120, mean_packet_size=100, duration_seconds=15, bytes_per_second=12_000), TrafficClass.REAL_TIME),
    (_flow(protocol="udp", packet_rate=40,  mean_packet_size=350, duration_seconds=8,  bytes_per_second=14_000), TrafficClass.REAL_TIME),

    # ---- INTERACTIVE: Gaming, burst UDP — higher pps, medium-large packets ---
    (_flow(protocol="udp", packet_rate=300, mean_packet_size=600,  duration_seconds=2, bytes_per_second=180_000), TrafficClass.INTERACTIVE),
    (_flow(protocol="udp", packet_rate=500, mean_packet_size=800,  duration_seconds=1, bytes_per_second=400_000), TrafficClass.INTERACTIVE),
    (_flow(protocol="udp", packet_rate=400, mean_packet_size=1000, duration_seconds=3, bytes_per_second=400_000), TrafficClass.INTERACTIVE),
    (_flow(protocol="udp", packet_rate=600, mean_packet_size=400,  duration_seconds=4, bytes_per_second=240_000), TrafficClass.INTERACTIVE),
    (_flow(protocol="udp", packet_rate=250, mean_packet_size=900,  duration_seconds=2, bytes_per_second=225_000), TrafficClass.INTERACTIVE),
    (_flow(protocol="udp", packet_rate=450, mean_packet_size=700,  duration_seconds=1, bytes_per_second=315_000), TrafficClass.INTERACTIVE),
    (_flow(protocol="udp", packet_rate=350, mean_packet_size=1100, duration_seconds=2, bytes_per_second=385_000), TrafficClass.INTERACTIVE),
    (_flow(protocol="udp", packet_rate=800, mean_packet_size=500,  duration_seconds=1, bytes_per_second=400_000), TrafficClass.INTERACTIVE),

    # ---- ADAPTIVE_VIDEO: DASH/HLS streaming — sustained TCP, moderate rate ---
    (_flow(protocol="tcp", packet_rate=50,  duration_seconds=20, bytes_per_second=1_500_000, mean_packet_size=1400), TrafficClass.ADAPTIVE_VIDEO),
    (_flow(protocol="tcp", packet_rate=80,  duration_seconds=30, bytes_per_second=1_800_000, mean_packet_size=1200), TrafficClass.ADAPTIVE_VIDEO),
    (_flow(protocol="tcp", packet_rate=60,  duration_seconds=15, bytes_per_second=1_200_000, mean_packet_size=1300), TrafficClass.ADAPTIVE_VIDEO),
    (_flow(protocol="quic", packet_rate=70, duration_seconds=25, bytes_per_second=1_600_000, mean_packet_size=1000), TrafficClass.ADAPTIVE_VIDEO),
    (_flow(protocol="tcp", packet_rate=55,  duration_seconds=12, bytes_per_second=900_000,   mean_packet_size=1100), TrafficClass.ADAPTIVE_VIDEO),
    (_flow(protocol="tcp", packet_rate=65,  duration_seconds=18, bytes_per_second=1_100_000, mean_packet_size=1350), TrafficClass.ADAPTIVE_VIDEO),
    (_flow(protocol="quic", packet_rate=90, duration_seconds=10, bytes_per_second=1_300_000, mean_packet_size=950),  TrafficClass.ADAPTIVE_VIDEO),
    (_flow(protocol="tcp", packet_rate=45,  duration_seconds=14, bytes_per_second=800_000,   mean_packet_size=1200), TrafficClass.ADAPTIVE_VIDEO),

    # ---- BULK: Large file download, backup — long-lived, high-volume TCP ---
    (_flow(protocol="tcp", packet_rate=1000, duration_seconds=30, bytes_per_second=10_000_000, mean_packet_size=1460), TrafficClass.BULK),
    (_flow(protocol="tcp", packet_rate=800,  duration_seconds=60, bytes_per_second=8_000_000,  mean_packet_size=1460), TrafficClass.BULK),
    (_flow(protocol="tcp", packet_rate=1200, duration_seconds=20, bytes_per_second=12_000_000, mean_packet_size=1400), TrafficClass.BULK),
    (_flow(protocol="tcp", packet_rate=900,  duration_seconds=45, bytes_per_second=9_000_000,  mean_packet_size=1460), TrafficClass.BULK),
    (_flow(protocol="tcp", packet_rate=1500, duration_seconds=15, bytes_per_second=15_000_000, mean_packet_size=1400), TrafficClass.BULK),
    (_flow(protocol="tcp", packet_rate=700,  duration_seconds=90, bytes_per_second=7_000_000,  mean_packet_size=1460), TrafficClass.BULK),
    (_flow(protocol="quic", packet_rate=1100, duration_seconds=25, bytes_per_second=11_000_000, mean_packet_size=1380), TrafficClass.BULK),
    (_flow(protocol="tcp", packet_rate=600,  duration_seconds=120, bytes_per_second=6_000_000, mean_packet_size=1460), TrafficClass.BULK),

    # ---- BACKGROUND: Software updates, sync — long-lived, low-rate ------
    (_flow(protocol="tcp", duration_seconds=120, bytes_per_second=100_000, packet_rate=10, mean_packet_size=1400), TrafficClass.BACKGROUND),
    (_flow(protocol="tcp", duration_seconds=300, bytes_per_second=200_000, packet_rate=15, mean_packet_size=1200), TrafficClass.BACKGROUND),
    (_flow(protocol="tcp", duration_seconds=180, bytes_per_second=50_000,  packet_rate=5,  mean_packet_size=1000), TrafficClass.BACKGROUND),
    (_flow(protocol="tcp", duration_seconds=600, bytes_per_second=300_000, packet_rate=20, mean_packet_size=1460), TrafficClass.BACKGROUND),
    (_flow(protocol="tcp", duration_seconds=240, bytes_per_second=150_000, packet_rate=12, mean_packet_size=1300), TrafficClass.BACKGROUND),
    (_flow(protocol="tcp", duration_seconds=90,  bytes_per_second=80_000,  packet_rate=8,  mean_packet_size=1100), TrafficClass.BACKGROUND),
    (_flow(protocol="tcp", duration_seconds=400, bytes_per_second=400_000, packet_rate=25, mean_packet_size=1400), TrafficClass.BACKGROUND),
    (_flow(protocol="tcp", duration_seconds=200, bytes_per_second=250_000, packet_rate=18, mean_packet_size=1200), TrafficClass.BACKGROUND),
]


def _evaluate() -> dict:
    """Evaluate classifier on the labelled dataset."""
    clf = MetadataClassifier()
    classes = list(TrafficClass)

    # confusion matrix: actual x predicted
    conf_matrix: dict[TrafficClass, dict[TrafficClass, int]] = {
        a: {p: 0 for p in classes} for a in classes
    }
    predictions: list[tuple[TrafficClass, TrafficClass, float]] = []

    for flow, actual in LABELLED_FLOWS:
        result = clf.classify(flow)
        predicted = result.category
        conf_matrix[actual][predicted] += 1
        predictions.append((actual, predicted, result.confidence))

    # Per-class metrics
    per_class: dict[str, dict] = {}
    total_correct = 0
    for cls in classes:
        tp = conf_matrix[cls][cls]
        fp = sum(conf_matrix[a][cls] for a in classes if a != cls)
        fn = sum(conf_matrix[cls][p] for p in classes if p != cls)
        tn = sum(conf_matrix[a][p] for a in classes for p in classes
                 if a != cls and p != cls)
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

    total = len(LABELLED_FLOWS)
    accuracy = total_correct / total if total > 0 else 0.0

    # Unknown-class rate (flows classified as UNKNOWN)
    unknown_count = sum(1 for _, predicted, _ in predictions if predicted == TrafficClass.UNKNOWN)
    unknown_rate = unknown_count / total if total > 0 else 0.0

    # Average confidence
    avg_confidence = sum(c for _, _, c in predictions) / total if total > 0 else 0.0

    # Macro-averaged metrics
    macro_precision = sum(v["precision"] for v in per_class.values()) / len(per_class)
    macro_recall = sum(v["recall"] for v in per_class.values()) / len(per_class)
    macro_f1 = sum(v["f1"] for v in per_class.values()) / len(per_class)

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_classifier_correctly_classifies_real_time_flows() -> None:
    clf = MetadataClassifier()
    rt_flows = [(f, a) for f, a in LABELLED_FLOWS if a == TrafficClass.REAL_TIME]
    correct = 0
    for flow, actual in rt_flows:
        result = clf.classify(flow)
        if result.category == TrafficClass.REAL_TIME:
            correct += 1
    recall = correct / len(rt_flows)
    assert recall >= 0.75, f"REAL_TIME recall too low: {recall:.0%} ({correct}/{len(rt_flows)})"


def test_classifier_correctly_classifies_bulk_flows() -> None:
    clf = MetadataClassifier()
    bulk_flows = [(f, a) for f, a in LABELLED_FLOWS if a == TrafficClass.BULK]
    correct = sum(1 for f, _ in bulk_flows if clf.classify(f).category == TrafficClass.BULK)
    recall = correct / len(bulk_flows)
    assert recall >= 0.75, f"BULK recall too low: {recall:.0%}"


def test_classifier_overall_accuracy_above_threshold() -> None:
    results = _evaluate()
    assert results["accuracy"] >= 0.60, (
        f"Overall accuracy {results['accuracy']:.0%} below 60% threshold. "
        f"Per-class: {results['per_class']}"
    )


def test_unknown_rate_is_low() -> None:
    results = _evaluate()
    assert results["unknown_rate"] <= 0.10, (
        f"Unknown classification rate {results['unknown_rate']:.0%} too high"
    )


def test_classifier_produces_confidence_scores() -> None:
    clf = MetadataClassifier()
    for flow, _ in LABELLED_FLOWS:
        result = clf.classify(flow)
        assert 0.0 <= result.confidence <= 1.0, f"Confidence out of range: {result.confidence}"
        assert result.reason_codes, "Every classification must have at least one reason code"


def test_classifier_never_inspects_payload() -> None:
    """Constraint verification: classifier only uses header metadata fields."""
    import inspect
    source = inspect.getsource(MetadataClassifier.classify)
    # Must NOT contain payload-related keywords
    forbidden = ["payload", "decrypt", "dpi", "deep_packet", "content", "http_body"]
    for kw in forbidden:
        assert kw not in source.lower(), f"Classifier source contains forbidden keyword: {kw!r}"


def test_classifier_confidence_allows_user_correction() -> None:
    """Verify that low-confidence classifications are flagged for potential correction."""
    clf = MetadataClassifier()
    # UNKNOWN should always have low confidence (< 0.5), marking it as correctable
    unknown_flow = _flow(protocol="tcp", duration_seconds=0.5, packet_rate=2, bytes_per_second=1_000)
    result = clf.classify(unknown_flow)
    # Unknown classification should have confidence <= 0.5
    assert result.confidence <= 0.5, f"UNKNOWN confidence should be low, got {result.confidence}"


def test_confusion_matrix_structure() -> None:
    results = _evaluate()
    classes = [c.value for c in TrafficClass]
    matrix = results["confusion_matrix"]
    for actual in classes:
        assert actual in matrix, f"Missing actual class {actual} in confusion matrix"
        for predicted in classes:
            assert predicted in matrix[actual], f"Missing cell [{actual}][{predicted}]"
            assert isinstance(matrix[actual][predicted], int)


def test_full_evaluation_report() -> None:
    """Integration test: run full evaluation and print human-readable report."""
    results = _evaluate()

    header = f"\n{'='*60}\nCLASSIFIER EVALUATION REPORT\n{'='*60}"
    print(header)
    print(f"Total samples: {results['total_samples']} | Correct: {results['correct']}")
    print(f"Accuracy:        {results['accuracy']:.1%}")
    print(f"Macro Precision: {results['macro_precision']:.1%}")
    print(f"Macro Recall:    {results['macro_recall']:.1%}")
    print(f"Macro F1:        {results['macro_f1']:.1%}")
    print(f"Unknown rate:    {results['unknown_rate']:.1%}")
    print(f"Avg confidence:  {results['avg_confidence']:.2f}")
    print(f"\nPer-class breakdown:")
    for cls, m in results["per_class"].items():
        if m["support"] > 0:
            print(f"  {cls:16s}  P={m['precision']:.2f}  R={m['recall']:.2f}  "
                  f"F1={m['f1']:.2f}  support={m['support']}")

    print(f"\nConfusion matrix (rows=actual, cols=predicted):")
    classes = [c.value for c in TrafficClass if results["confusion_matrix"].get(c.value)]
    header_row = f"{'':18s}" + "".join(f"{c[:6]:8s}" for c in [c.value for c in TrafficClass])
    print(header_row)
    for actual in TrafficClass:
        row = results["confusion_matrix"].get(actual.value, {})
        if any(row.values()):
            line = f"  {actual.value:16s}" + "".join(f"{row.get(p.value, 0):8d}" for p in TrafficClass)
            print(line)
    print(f"\nNote: This classifier uses ONLY Layer-3/4 header metadata.")
    print(f"      No payload inspection. Zero DPI. Constraint C1 satisfied.")
    print("=" * 60)

    assert results["accuracy"] >= 0.60
