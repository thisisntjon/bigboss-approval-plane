import unittest

from bigboss.router.meter import UsageMeter


STREAM = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"id":"msg_1","model":"claude-opus-4-8",'
    b'"usage":{"input_tokens":100,"output_tokens":1,"cache_read_input_tokens":10,'
    b'"cache_creation_input_tokens":0}}}\n\n'
    b"event: ping\ndata: {\"type\":\"ping\"}\n\n"
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
    b"event: message_delta\n"
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":250}}\n\n'
    b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
)


class MeterTests(unittest.TestCase):
    def test_streaming_single_chunk(self):
        meter = UsageMeter()
        meter.feed(STREAM)
        u = meter.result()
        self.assertEqual(u.served_model, "claude-opus-4-8")
        self.assertEqual(u.input_tokens, 100)
        self.assertEqual(u.output_tokens, 250)  # cumulative from final message_delta, not summed
        self.assertEqual(u.cache_read_input_tokens, 10)
        self.assertEqual(u.stop_reason, "end_turn")

    def test_streaming_byte_by_byte_is_split_frame_robust(self):
        meter = UsageMeter()
        for i in range(len(STREAM)):
            meter.feed(STREAM[i : i + 1])
        u = meter.result()
        self.assertEqual(u.served_model, "claude-opus-4-8")
        self.assertEqual(u.input_tokens, 100)
        self.assertEqual(u.output_tokens, 250)
        self.assertEqual(u.stop_reason, "end_turn")

    def test_reroute_refusal_captured(self):
        # Requested Fable, Anthropic served Opus and refused -> meter sees served + refusal.
        stream = (
            b'data: {"type":"message_start","message":{"model":"claude-opus-4-8",'
            b'"usage":{"input_tokens":80}}}\n\n'
            b'data: {"type":"message_delta","delta":{"stop_reason":"refusal"},'
            b'"usage":{"output_tokens":5}}\n\n'
        )
        meter = UsageMeter()
        meter.feed(stream)
        u = meter.result()
        self.assertEqual(u.served_model, "claude-opus-4-8")
        self.assertEqual(u.stop_reason, "refusal")

    def test_non_stream_message_object(self):
        body = (
            b'{"type":"message","model":"claude-haiku-4-5-20251001","stop_reason":"end_turn",'
            b'"usage":{"input_tokens":50,"output_tokens":20,"cache_read_input_tokens":5,'
            b'"cache_creation_input_tokens":0}}'
        )
        meter = UsageMeter()
        meter.feed_full(body)
        u = meter.result()
        self.assertEqual(u.served_model, "claude-haiku-4-5-20251001")
        self.assertEqual(u.input_tokens, 50)
        self.assertEqual(u.output_tokens, 20)
        self.assertEqual(u.stop_reason, "end_turn")

    def test_cache_creation_breakdown_captured(self):
        body = (
            b'{"type":"message","model":"claude-opus-4-8","usage":{"input_tokens":10,'
            b'"cache_creation_input_tokens":300,'
            b'"cache_creation":{"ephemeral_5m_input_tokens":100,"ephemeral_1h_input_tokens":200}}}'
        )
        meter = UsageMeter()
        meter.feed_full(body)
        u = meter.result()
        self.assertEqual(u.cache_creation["ephemeral_1h_input_tokens"], 200)
        self.assertEqual(u.as_pricing_usage()["cache_creation"]["ephemeral_5m_input_tokens"], 100)


if __name__ == "__main__":
    unittest.main()
