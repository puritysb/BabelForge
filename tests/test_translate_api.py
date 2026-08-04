from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import translate


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class TranslateApiTests(unittest.TestCase):
    def test_json_mode_uses_provider_structured_output_contract(self):
        response = _Response({
            "choices": [{
                "message": {"content": '{"ok":true}'},
                "finish_reason": "stop",
            }]
        })
        with patch.object(translate.config, "get_zai_api_key", return_value="test"), \
                patch.object(translate.urllib.request, "urlopen",
                             return_value=response) as urlopen:
            raw = translate._chat_json([
                {"role": "system", "content": "Return JSON."},
                {"role": "user", "content": "{}"},
            ])

        self.assertEqual('{"ok":true}', raw)
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual({"type": "json_object"}, body["response_format"])
        self.assertEqual({"type": "disabled"}, body["thinking"])

    def test_empty_message_is_rejected_before_http(self):
        with patch.object(translate.config, "get_zai_api_key", return_value="test"), \
                patch.object(translate.urllib.request, "urlopen") as urlopen:
            with self.assertRaisesRegex(translate.TranslationError, "empty"):
                translate._chat_json([
                    {"role": "system", "content": "Return JSON."},
                    {"role": "user", "content": ""},
                ])
        urlopen.assert_not_called()

    def test_business_code_1213_is_not_replayed_unchanged(self):
        error = urllib.error.HTTPError(
            url="https://api.z.ai/test",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(
                b'{"error":{"code":"1213","message":"prompt parameter not received normally"}}'
            ),
        )
        with patch.object(translate.config, "get_zai_api_key", return_value="test"), \
                patch.object(translate.urllib.request, "urlopen",
                             side_effect=error) as urlopen:
            with self.assertRaisesRegex(translate.TranslationError,
                                        "business_code=1213"):
                translate._chat_json([
                    {"role": "system", "content": "Return JSON."},
                    {"role": "user", "content": "{}"},
                ])
        self.assertEqual(1, urlopen.call_count)


if __name__ == "__main__":
    unittest.main()
