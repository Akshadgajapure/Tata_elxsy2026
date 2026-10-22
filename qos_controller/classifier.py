"""Explainable metadata-only traffic classification."""

from .models import Classification, FlowMetadata, TrafficClass


class MetadataClassifier:
    """Classify broad traffic categories without inspecting payloads."""

    def classify(self, flow: FlowMetadata) -> Classification:
        reasons: list[str] = []
        protocol = flow.protocol.lower()

        if flow.dscp in {46,  ef_dscp()}:
            return Classification(
                TrafficClass.REAL_TIME, 0.98, ("expedited_forwarding_dscp",)
            )

        if (
            protocol == "udp"
            and 20 <= flow.packet_rate <= 250
            and flow.mean_packet_size <= 400
            and flow.duration_seconds >= 2
        ):
            reasons.extend(("regular_small_udp_packets", "sustained_flow"))
            return Classification(TrafficClass.REAL_TIME, 0.84, tuple(reasons))

        if (
            protocol == "udp"
            and flow.packet_rate > 5
            and flow.mean_packet_size <= 1200
        ):
            return Classification(
                TrafficClass.INTERACTIVE,
                0.68,
                ("latency_sensitive_udp",),
            )

        if (
            protocol in {"tcp", "quic"}
            and flow.duration_seconds >= 10
            and flow.bytes_per_second >= 2_000_000
        ):
            return Classification(
                TrafficClass.BULK,
                0.78,
                ("long_lived_high_volume_flow",),
            )

        if protocol in {"tcp", "quic"} and flow.duration_seconds >= 5:
            return Classification(
                TrafficClass.ADAPTIVE_VIDEO,
                0.55,
                ("long_lived_elastic_flow",),
            )

        if flow.duration_seconds >= 60 and flow.bytes_per_second < 500_000:
            return Classification(
                TrafficClass.BACKGROUND,
                0.52,
                ("long_low_rate_flow",),
            )

        return Classification(TrafficClass.UNKNOWN, 0.30, ("insufficient_metadata",))


def ef_dscp() -> int:
    """Return the decimal DSCP value for Expedited Forwarding."""

    return 46
