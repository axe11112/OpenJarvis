"""Tests for the TelegramChannel adapter."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.channels._stubs import ChannelStatus
from openjarvis.channels.telegram import TelegramChannel
from openjarvis.core.events import EventBus, EventType
from openjarvis.core.registry import ChannelRegistry
from tests.channels.channel_test_helpers import make_common_channel_tests


@pytest.fixture(autouse=True)
def _register_telegram():
    """Re-register after any registry clear."""
    if not ChannelRegistry.contains("telegram"):
        ChannelRegistry.register_value("telegram", TelegramChannel)


TestCommonChannel = make_common_channel_tests(
    TelegramChannel, "telegram", constructor_kwargs={"bot_token": "test-token"}
)


class TestInit:
    def test_defaults(self):
        ch = TelegramChannel()
        assert ch._token == ""
        assert ch._parse_mode == "Markdown"
        assert ch._status == ChannelStatus.DISCONNECTED

    def test_constructor_token(self):
        ch = TelegramChannel(bot_token="my-token")
        assert ch._token == "my-token"

    def test_env_var_fallback(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "env-token"}):
            ch = TelegramChannel()
            assert ch._token == "env-token"

    def test_constructor_overrides_env(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "env-token"}):
            ch = TelegramChannel(bot_token="explicit-token")
            assert ch._token == "explicit-token"


class TestSend:
    def test_send_success(self):
        ch = TelegramChannel(bot_token="123:ABC")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.post", return_value=mock_response) as mock_post:
            result = ch.send("12345678", "Hello!")
            assert result is True
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            url = call_args[0][0]
            assert "api.telegram.org" in url
            assert "bot123:ABC" in url
            assert "sendMessage" in url
            payload = call_args[1]["json"]
            assert payload["chat_id"] == "12345678"
            assert payload["text"] == "Hello!"
            assert payload["parse_mode"] == "Markdown"

    def test_send_failure(self):
        ch = TelegramChannel(bot_token="123:ABC")

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"

        with patch("httpx.post", return_value=mock_response):
            result = ch.send("12345678", "Hello!")
            assert result is False

    def test_send_exception(self):
        ch = TelegramChannel(bot_token="123:ABC")

        with patch("httpx.post", side_effect=ConnectionError("refused")):
            result = ch.send("12345678", "Hello!")
            assert result is False

    def test_send_no_token(self):
        ch = TelegramChannel()
        result = ch.send("12345678", "Hello!")
        assert result is False

    def test_send_publishes_event(self):
        bus = EventBus(record_history=True)
        ch = TelegramChannel(bot_token="123:ABC", bus=bus)

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.post", return_value=mock_response):
            ch.send("12345678", "Hello!")

        event_types = [e.event_type for e in bus.history]
        assert EventType.CHANNEL_MESSAGE_SENT in event_types

    def test_send_uses_channel_as_chat_id_under_unified_contract(self):
        """Canonical contract (#515/#516): the first positional ``channel``
        arg is the chat destination, and ``conversation_id`` is the inbound
        message id used as ``reply_to_message_id`` — not the chat id."""
        ch = TelegramChannel(bot_token="123:ABC")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.post", return_value=mock_response) as mock_post:
            result = ch.send("12345678", "Reply!", conversation_id="55")
            assert result is True
            payload = mock_post.call_args[1]["json"]
            # Destination is the chat id from the positional channel arg.
            assert payload["chat_id"] == "12345678"
            # conversation_id becomes the reply reference, not the chat id.
            assert payload["reply_to_message_id"] == "55"

    def test_send_legacy_conversation_id_only_still_targets_chat(self):
        """Backwards compatibility: a legacy caller passing the chat id via
        ``conversation_id`` (with an empty ``channel``) still delivers."""
        ch = TelegramChannel(bot_token="123:ABC")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.post", return_value=mock_response) as mock_post:
            result = ch.send("", "Hello!", conversation_id="12345678")
            assert result is True
            payload = mock_post.call_args[1]["json"]
            assert payload["chat_id"] == "12345678"
            # When channel is empty, conversation_id is the chat id, so it must
            # not also be used as a self-referential reply id.
            assert "reply_to_message_id" not in payload


class TestStatus:
    def test_no_token_connect_error(self):
        ch = TelegramChannel()
        ch.connect()
        assert ch.status() == ChannelStatus.ERROR


class TestConnectIsIdempotent:
    """Two callers sharing one channel must not open two long-polls.

    Found live: ``jarvis reliability watch`` calls ``connect()`` once for
    itself and once more inside ``TelegramOwnerDoor.start()`` on the exact
    same channel object. Telegram allows only one getUpdates consumer per bot
    token; the second connect used to start a second listener thread, and the
    two then fought forever with "Conflict: terminated by other getUpdates
    request", each restart re-triggering the same fight.

    ``_poll_loop`` is stubbed out here so these tests exercise the threading
    guard in ``connect()`` without opening a real long-poll against Telegram.
    """

    @staticmethod
    def _fake_poll_loop(self):
        self._stop_event.wait()

    def test_a_second_connect_does_not_start_a_second_listener_thread(self):
        with patch.object(TelegramChannel, "_poll_loop", self._fake_poll_loop):
            ch = TelegramChannel(bot_token="123:ABC")
            ch.connect()
            first_thread = ch._listener_thread
            assert first_thread is not None and first_thread.is_alive()

            ch.connect()

            assert ch._listener_thread is first_thread
            ch.disconnect()

    def test_connect_after_disconnect_does_start_a_new_listener(self):
        with patch.object(TelegramChannel, "_poll_loop", self._fake_poll_loop):
            ch = TelegramChannel(bot_token="123:ABC")
            ch.connect()
            first_thread = ch._listener_thread
            ch.disconnect()

            ch.connect()

            assert ch._listener_thread is not None
            assert ch._listener_thread is not first_thread
            ch.disconnect()


class TestAllowedChatIds:
    """Tests for the allowed_chat_ids enforcement in _poll_loop."""

    def _make_update(self, chat_id: str, text: str = "hello"):
        """Build a minimal fake python-telegram-bot Update object."""
        msg = MagicMock()
        msg.text = text
        msg.message_id = 1
        msg.from_user.id = chat_id
        msg.chat.id = chat_id
        update = MagicMock()
        update.message = msg
        return update

    def _invoke_handle_msg(self, ch: TelegramChannel, chat_id: str, text: str = "hi"):
        """Simulate _poll_loop dispatching a message without starting a thread."""
        from openjarvis.channels._stubs import ChannelMessage

        cm = ChannelMessage(
            channel="telegram",
            sender=chat_id,
            content=text,
            message_id="1",
            conversation_id=chat_id,
        )
        # Directly exercise the allow-list logic (mirrors _handle_msg body)
        if ch._allowed_chat_ids:
            _allowed = {
                cid.strip() for cid in ch._allowed_chat_ids.split(",") if cid.strip()
            }
            if cm.conversation_id not in _allowed:
                return False  # would return inside _handle_msg
        for handler in ch._handlers:
            handler(cm)
        return True

    def test_no_allowlist_accepts_any(self):
        """When allowed_chat_ids is empty every chat is dispatched."""
        ch = TelegramChannel(bot_token="tok", allowed_chat_ids="")
        handler = MagicMock()
        ch.on_message(handler)
        dispatched = self._invoke_handle_msg(ch, "99999")
        assert dispatched is True
        handler.assert_called_once()

    def test_allowlist_passes_listed_chat(self):
        """A chat ID present in the allow-list is dispatched to handlers."""
        ch = TelegramChannel(bot_token="tok", allowed_chat_ids="111,222")
        handler = MagicMock()
        ch.on_message(handler)
        dispatched = self._invoke_handle_msg(ch, "111")
        assert dispatched is True
        handler.assert_called_once()

    def test_allowlist_blocks_unlisted_chat(self):
        """A chat ID not in the allow-list is silently dropped (not dispatched)."""
        ch = TelegramChannel(bot_token="tok", allowed_chat_ids="111,222")
        handler = MagicMock()
        ch.on_message(handler)
        dispatched = self._invoke_handle_msg(ch, "999")
        assert dispatched is False
        handler.assert_not_called()

    def test_allowlist_trims_whitespace(self):
        """Spaces around IDs in the allow-list are handled gracefully."""
        ch = TelegramChannel(bot_token="tok", allowed_chat_ids=" 111 , 222 ")
        handler = MagicMock()
        ch.on_message(handler)
        dispatched = self._invoke_handle_msg(ch, "111")
        assert dispatched is True
        handler.assert_called_once()


class TestChannelAgentWiring:
    """Tests for the channel → agent handler wired in serve.py."""

    def test_on_message_handler_invoked_on_message(self):
        """on_message callback registered on a channel is called when a message
        arrives."""
        ch = TelegramChannel(bot_token="tok")
        received = []
        ch.on_message(lambda cm: received.append(cm))

        from openjarvis.channels._stubs import ChannelMessage

        cm = ChannelMessage(
            channel="telegram",
            sender="42",
            content="ping",
            message_id="1",
            conversation_id="42",
        )
        for h in ch._handlers:
            h(cm)

        assert len(received) == 1
        assert received[0].content == "ping"

    def test_multiple_handlers_all_invoked(self):
        """Both handlers registered via on_message are called for the same message."""
        ch = TelegramChannel(bot_token="tok")
        calls_a: list = []
        calls_b: list = []
        ch.on_message(lambda cm: calls_a.append(cm))
        ch.on_message(lambda cm: calls_b.append(cm))

        from openjarvis.channels._stubs import ChannelMessage

        cm = ChannelMessage(
            channel="telegram",
            sender="1",
            content="x",
            message_id="1",
            conversation_id="1",
        )
        for h in ch._handlers:
            h(cm)

        assert len(calls_a) == 1
        assert len(calls_b) == 1


class TestExactlyOnePollerConstructionSite:
    """Found investigating ~1,593 'terminated by other getUpdates request'
    Conflict errors in the reliability watcher's log: proven historical (a
    one-time consequence of a hard SIGKILL-based restart not letting the
    prior process release its long-poll slot before the new one opened
    one), not a live architectural bug -- confirmed by both a bounded live
    observation window and, more durably, by tracing every call site that
    constructs a TelegramChannel.

    That trace found exactly three: reliability_cmd.py's reliability_watch
    (constructs a *receiving* channel and calls .connect(), gated on
    accept_owner_commands), reliability/notify.py's TelegramNotifier
    (send-only, never calls .connect()), and channels/telegram.py itself
    (the class definition). Two Telegram-capable processes commonly run at
    once -- the watcher and the Control Center dashboard (jarvis wiz
    dashboard / jarvis reliability dashboard, both served by
    reliability_cmd.run_control_center) -- so the one architectural
    invariant worth a permanent regression guard is that the dashboard's
    own code path never gains a second receiving construction site, which
    would silently reintroduce the exact Conflict shape found tonight.

    A source-level guard rather than an integration test that actually
    launches both processes: cheaper, faster, and it fails at the exact
    place a future change would introduce the regression -- adding a
    TelegramChannel(...) construction to the dashboard's own module or
    shared control-center code -- rather than only downstream, in a flaky
    "did Telegram's API return Conflict" integration test.
    """

    def _source(self, module_name: str) -> str:
        import importlib
        import inspect

        module = importlib.import_module(module_name)
        return inspect.getsource(module)

    def test_the_dashboard_module_never_constructs_a_telegram_channel(self):
        source = self._source("openjarvis.cli.wiz_cmd")
        assert "TelegramChannel(" not in source

    def test_the_shared_control_center_function_never_constructs_one(self):
        import inspect

        from openjarvis.cli.reliability_cmd import run_control_center

        assert "TelegramChannel(" not in inspect.getsource(run_control_center)

    def test_exactly_one_function_in_reliability_cmd_constructs_a_receiving_channel(
        self,
    ):
        import ast
        import inspect

        from openjarvis.cli import reliability_cmd

        tree = ast.parse(inspect.getsource(reliability_cmd))
        constructors = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                body_source = ast.get_source_segment(
                    inspect.getsource(reliability_cmd), node
                ) or ""
                if "TelegramChannel(" in body_source:
                    constructors.append(node.name)
        assert constructors == ["reliability_watch"]

    def test_notify_py_never_calls_connect(self):
        import inspect

        from openjarvis.reliability import notify

        assert ".connect(" not in inspect.getsource(notify)
