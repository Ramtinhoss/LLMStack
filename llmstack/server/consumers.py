import asyncio
import audioop
import base64
import importlib
import json
import logging
import uuid

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, QueryDict
from flags.state import flag_enabled

from llmstack.apps.runner.app_runner import (
    AppRunnerRequest,
    AppRunnerStreamingResponseType,
    PlaygroundAppRunnerSource,
    StoreAppRunnerSource,
    TwilioAppRunnerSource,
    WebAppRunnerSource,
)
from llmstack.assets.utils import get_asset_by_objref
from llmstack.connections.actors import ConnectionActivationActor
from llmstack.connections.models import (
    Connection,
    ConnectionActivationInput,
    ConnectionActivationOutput,
    ConnectionStatus,
)
from llmstack.events.apis import JSONEncoder
from llmstack.play.utils import run_coro_in_new_loop

logger = logging.getLogger(__name__)

usage_limiter_module = importlib.import_module(settings.LIMITER_MODULE)
is_ratelimited_fn = getattr(usage_limiter_module, "is_ratelimited", None)
is_usage_limited_fn = getattr(usage_limiter_module, "is_usage_limited", None)


class UsageLimitReached(PermissionDenied):
    pass


class OutOfCredits(PermissionDenied):
    pass


@database_sync_to_async
def _usage_limit_exceeded(request, user):
    return flag_enabled(
        "HAS_EXCEEDED_MONTHLY_PROCESSOR_RUN_QUOTA",
        request=request,
        user=user,
    )


@database_sync_to_async
def _build_request_from_input(post_data, scope):
    session = dict(scope["session"])
    headers = dict(scope["headers"])
    content_type = headers.get(
        b"content-type",
        b"application/json",
    ).decode("utf-8")
    path_info = scope.get("path", "")
    method = scope.get("method", "")
    query_string = scope.get("query_string", b"").decode("utf-8")
    query_params = QueryDict(query_string)
    user = scope.get("user")

    http_request = HttpRequest()
    http_request.META = {
        "CONTENT_TYPE": content_type,
        "PATH_INFO": path_info,
        "QUERY_STRING": query_string,
        "HTTP_USER_AGENT": headers.get(
            b"user-agent",
            b"",
        ).decode("utf-8"),
        "REMOTE_ADDR": headers.get(b"x-forwarded-for", b"").decode("utf-8").split(",")[0].strip(),
        "_prid": session.get("_prid", ""),
    }
    http_request.session = session
    http_request.method = method
    http_request.GET = query_params
    http_request.query_params = query_params
    http_request.stream = json.dumps(post_data)
    http_request.user = user
    http_request.data = post_data

    return http_request


class AppConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        from llmstack.apps.apis import AppViewSet

        self._app_uuid = self.scope["url_route"]["kwargs"]["app_uuid"]
        self._preview = True if "preview" in self.scope["url_route"]["kwargs"] else False
        self._session_id = str(uuid.uuid4())
        self._user = self.scope.get("user", None)

        headers = dict(self.scope["headers"])
        request_ip = headers.get(
            "X-Forwarded-For",
            self.scope.get("client", [""])[0] or "",
        ).split(",")[
            0
        ].strip() or headers.get("X-Real-IP", "")
        request_location = headers.get("X-Client-Geo-Location", "")
        request_user_email = self._user.email if self._user and not self._user.is_anonymous else None

        self._source = WebAppRunnerSource(
            id=self._session_id,
            request_ip=request_ip,
            request_location=request_location,
            request_user_agent=headers.get("User-Agent", ""),
            request_content_type=headers.get("Content-Type", ""),
            app_uuid=self._app_uuid,
            request_user_email=request_user_email,
            request_user=self._user,
        )
        self._app_runner = await AppViewSet().get_app_runner_async(
            self._session_id,
            self._app_uuid,
            self._source,
            self.scope.get("user", None),
            self._preview,
        )
        self._event_response_task = None
        self._connected = True
        await self.accept()

    async def disconnect(self, close_code):
        self._connected = False
        if self._app_runner:
            await self._app_runner.stop()

    async def stop(self):
        self._connected = False
        if self._event_response_task:
            self._event_response_task.cancel()
        await self.close()

    async def _respond_to_event(self, text_data):
        json_data = json.loads(text_data)
        client_request_id = json_data.get("id", None)
        event = json_data.get("event", None)

        if event == "run":
            app_runner_request = AppRunnerRequest(
                client_request_id=client_request_id,
                session_id=self._session_id,
                input=json_data.get("input", {}),
            )
            try:
                response_iterator = self._app_runner.run(app_runner_request)
                async for response in response_iterator:
                    # Check both cancellation and connection state
                    if asyncio.current_task().cancelled() or not self._connected:
                        break

                    if response.type == AppRunnerStreamingResponseType.OUTPUT_STREAM_CHUNK:
                        await self.send(text_data=response.model_dump_json())
                    elif response.type == AppRunnerStreamingResponseType.ERRORS:
                        await self.send(
                            text_data=json.dumps(
                                {
                                    "errors": [error.message for error in response.data.errors],
                                    "request_id": client_request_id,
                                }
                            )
                        )
                    elif response.type == AppRunnerStreamingResponseType.OUTPUT_STREAM_END:
                        await self.send(text_data=json.dumps({"event": "done", "request_id": client_request_id}))
            except Exception as e:
                logger.exception(f"Failed to run app: {e}")
        elif event == "create_asset":
            from llmstack.apps.models import AppSessionFiles

            try:
                asset_data = json_data.get("data", {})
                asset_metadata = {
                    "file_name": asset_data.get("file_name", str(uuid.uuid4())),
                    "mime_type": asset_data.get("mime_type", "application/octet-stream"),
                    "app_uuid": self.app_id,
                    "username": (
                        self._user.username
                        if self._user and not self._user.is_anonymous
                        else self._session.get("_prid", "")
                    ),
                }

                asset = await sync_to_async(AppSessionFiles.create_asset)(
                    asset_metadata, self._session_id, streaming=asset_data.get("streaming", False)
                )

                if not asset:
                    await self.send(
                        text_data=json.dumps(
                            {
                                "errors": ["Failed to create asset"],
                                "reply_to": client_request_id,
                                "request_id": client_request_id,
                                "asset_request_id": client_request_id,
                            }
                        )
                    )
                    return

                output = {
                    "asset": asset.objref,
                    "reply_to": client_request_id,
                    "request_id": client_request_id,
                    "asset_request_id": client_request_id,
                }

                await self.send(text_data=json.dumps(output))
            except Exception as e:
                logger.exception(e)
                await self.send(
                    text_data=json.dumps(
                        {
                            "errors": [str(e)],
                            "reply_to": client_request_id,
                            "request_id": client_request_id,
                            "asset_request_id": client_request_id,
                        }
                    )
                )

        if event == "delete_asset":
            # Delete an asset in the session
            if not self._session_id:
                return

            # TODO: Implement delete asset
        elif event == "stop":
            await self.stop()

    async def receive(self, text_data):
        self._event_response_task = run_coro_in_new_loop(self._respond_to_event(text_data), name="respond_to_event")


class AssetStreamConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self._category = self.scope["url_route"]["kwargs"]["category"]
        self._uuid = self.scope["url_route"]["kwargs"]["uuid"]
        self._session = self.scope["session"]
        self._request_user = self.scope["user"]
        self._asset = await sync_to_async(get_asset_by_objref)(
            f"objref://{self._category}/{self._uuid}", self._request_user, self._session
        )
        await self.accept()

    async def disconnect(self, close_code):
        pass

    async def _respond_to_event(self, bytes_data):
        from llmstack.assets.stream import AssetStream

        if bytes_data:
            # Using b"\n" as delimiter
            chunks = bytes_data.split(b"\n")
            event = chunks[0]

            if not self._asset:
                # Close the connection
                await self.close(code=1008)
                return

            asset_stream = AssetStream(self._asset)

            try:
                if event == b"read":
                    async for chunk in asset_stream.read_async(start_index=0):
                        await self.send(bytes_data=chunk)

                if event == b"write":
                    if bytes_data == b"write\n":
                        await sync_to_async(asset_stream.finalize)()
                        await self.close()
                        return

                    await sync_to_async(asset_stream.append_chunk)(bytes_data[6:])

            except Exception as e:
                logger.exception(e)
                await self.send(bytes_data=b"")
                await self.close()

    async def receive(self, text_data=None, bytes_data=None):
        run_coro_in_new_loop(self._respond_to_event(bytes_data))


class ConnectionConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope["user"]
        self._activation_task = None

        if self.user.is_anonymous:
            await self.close()
            return

        self.connection_id = self.scope["url_route"]["kwargs"]["conn_id"]
        self.connection_activation_actor = ConnectionActivationActor.start(
            self.user,
            self.connection_id,
        ).proxy()
        await self.accept()

    async def disconnect(self, close_code):
        self.connection_activation_actor.stop()
        if self._activation_task and not self._activation_task.done():
            self._activation_task.cancel()
        self.close(code=close_code)

    async def _activate_connection(self):
        try:
            output = await self.connection_activation_actor.activate()
            async for c in output:
                if isinstance(c, Connection):
                    if c.status == ConnectionStatus.ACTIVE:
                        await self.connection_activation_actor.set_connection(c)
                    await self.send(
                        text_data=json.dumps(
                            {"event": "success" if c.status == ConnectionStatus.ACTIVE else "error"},
                        ),
                    )
                    self.connection_activation_actor.stop()
                elif isinstance(c, ConnectionActivationOutput):
                    await self.send(
                        text_data=json.dumps(
                            {"event": "output", "output": c.data},
                        ),
                    )
                elif isinstance(c, dict):
                    connection = c.get("connection", None)
                    if connection:
                        await self.connection_activation_actor.set_connection(connection)
                    if c.get("error", None):
                        await self.send(
                            text_data=json.dumps(
                                {"event": "error", "error": c.get("error")},
                            ),
                        )
                await asyncio.sleep(0.01)
        except Exception as e:
            logger.exception(e)
            self.connection_activation_actor.stop()

    async def _handle_input(self, text_data=None, bytes_data=None):
        try:
            await self.connection_activation_actor.input(ConnectionActivationInput(data=text_data)).get()
        except Exception as e:
            logger.exception(e)

    async def receive(self, text_data=None, bytes_data=None):
        json_data = json.loads(text_data or "{}")
        input = json_data.get("input", {})
        event = json_data.get("event", None)

        if event == "activate":
            loop = asyncio.get_running_loop()
            self._activation_task = loop.create_task(
                self._activate_connection(),
            )

        if event == "input" and input == "terminate":
            try:
                self.connection_activation_actor.input(
                    ConnectionActivationInput(data=input),
                )
            except Exception as e:
                logger.exception(e)
                pass
            finally:
                self.disconnect(1000)


class PlaygroundConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        headers = dict(self.scope["headers"])
        request_ip = headers.get("X-Forwarded-For", self.scope.get("client", [""])[0] or "").split(",")[
            0
        ].strip() or headers.get("X-Real-IP", "")
        request_location = headers.get("X-Client-Geo-Location", "")
        request_user_email = self.scope.get("user", None).email if self.scope.get("user", None) else None

        self._source = PlaygroundAppRunnerSource(
            request_ip=request_ip,
            request_location=request_location,
            request_user_agent=headers.get("User-Agent", ""),
            request_content_type=headers.get("Content-Type", ""),
            request_user_email=request_user_email,
            request_user=self.scope.get("user", None),
            processor_slug="",
            provider_slug="",
        )
        self._event_response_task = None
        self._app_runner = None
        self._connected = True
        await self.accept()

    async def disconnect(self, close_code):
        self._connected = False
        if self._event_response_task:
            self._event_response_task.cancel()
        if self._app_runner:
            await self._app_runner.stop()

    async def _respond_to_event(self, text_data):
        from llmstack.apps.apis import PlaygroundViewSet

        json_data = json.loads(text_data)
        event_input = json_data.get("input", {})

        processor_slug = event_input.get("api_backend_slug")
        provider_slug = event_input.get("api_provider_slug")
        input_data = event_input.get("input", {})
        config_data = event_input.get("config", {})

        session_id = str(uuid.uuid4())
        source = self._source.model_copy(
            update={
                "session_id": session_id,
                "processor_slug": processor_slug,
                "provider_slug": provider_slug,
            }
        )

        client_request_id = json_data.get("id", None)
        app_runner_request = AppRunnerRequest(
            client_request_id=client_request_id, session_id=session_id, input=input_data
        )

        self._app_runner = await PlaygroundViewSet().get_app_runner_async(
            session_id, source, self.scope.get("user", None), input_data, config_data
        )
        try:
            response_iterator = self._app_runner.run(app_runner_request)
            async for response in response_iterator:
                if not self._connected:
                    break

                if response.type == AppRunnerStreamingResponseType.OUTPUT_STREAM_CHUNK:
                    await self.send(text_data=response.model_dump_json())
                elif response.type == AppRunnerStreamingResponseType.OUTPUT:
                    await self.send(
                        text_data=json.dumps(
                            {"event": "done", "request_id": client_request_id, "data": response.data.chunks},
                            cls=JSONEncoder,
                        )
                    )
        except Exception as e:
            logger.exception(f"Failed to run app: {e}")
        await self._app_runner.stop()

    async def receive(self, text_data):
        self._event_response_task = run_coro_in_new_loop(self._respond_to_event(text_data))


class StoreAppConsumer(AppConsumer):
    async def connect(self):
        from llmstack.app_store.apis import AppStoreAppViewSet

        self._app_slug = self.scope["url_route"]["kwargs"]["app_id"]
        self._session_id = str(uuid.uuid4())

        headers = dict(self.scope["headers"])
        request_ip = headers.get("X-Forwarded-For", self.scope.get("client", [""])[0] or "").split(",")[
            0
        ].strip() or headers.get("X-Real-IP", "")
        request_location = headers.get("X-Client-Geo-Location", "")
        request_user_email = self.scope.get("user", None).email if self.scope.get("user", None) else None

        self._source = StoreAppRunnerSource(
            slug=self._app_slug,
            request_ip=request_ip,
            request_location=request_location,
            request_user_agent=headers.get("User-Agent", ""),
            request_user_email=request_user_email,
            request_user=self.scope.get("user", None),
        )

        self._app_runner = await AppStoreAppViewSet().get_app_runner_async(
            self._session_id, self._app_slug, self._source, self.scope.get("user", None)
        )
        self._connected = True
        self._event_response_task = None
        await self.accept()


class TwilioVoiceConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        from llmstack.apps.apis import AppViewSet

        self._app_uuid = self.scope["url_route"]["kwargs"]["app_uuid"]
        self._session_id = str(uuid.uuid4())
        self._source = TwilioAppRunnerSource(
            app_uuid=self._app_uuid, incoming_number=self.scope["url_route"]["kwargs"]["incoming_number"]
        )
        self._input_audio_stream = None
        self._output_audio_task = None

        headers = dict(self.scope["headers"])

        twilio_signature = headers.get(b"x-twilio-signature", b"").decode("utf-8")
        logger.info(f"Twilio signature to verify: {twilio_signature}")

        # TODO: Verify the twilio signature

        self._app_runner = await AppViewSet().get_app_runner_async(
            self._session_id,
            self._app_uuid,
            self._source,
            self.scope.get("user", None),
            app_data_config_override={
                "input_audio_format": "pcm16",
                "output_audio_format": "g711_ulaw",
            },
        )

        self._connected = True
        self._event_response_task = None
        await self.accept()

    async def disconnect(self, close_code):
        self._connected = False
        if self._app_runner:
            await self._app_runner.stop()
        await self.close(code=close_code)

    async def receive(self, text_data):
        self._event_response_task = run_coro_in_new_loop(self._respond_to_event(text_data))

    async def _respond_to_event(self, text_data):
        from llmstack.assets.stream import AssetStream

        json_data = json.loads(text_data)
        event = json_data.get("event", None)

        if event == "start":
            # Run the app
            self._stream_sid = json_data.get("start", {}).get("streamSid", "")
            response_iterator = self._app_runner.run(
                AppRunnerRequest(
                    client_request_id=self._stream_sid,
                    session_id=self._session_id,
                    input={},
                )
            )

            # Iterate till we get the objrefs for input and output audio
            async for response in response_iterator:
                if not self._connected:
                    break

                if response.type == AppRunnerStreamingResponseType.OUTPUT_STREAM_CHUNK:
                    deltas = response.data.deltas
                    if "agent_input_audio_stream" in deltas:
                        input_audio_stream_objref = deltas["agent_input_audio_stream"][1:]
                        input_audio_stream = await sync_to_async(get_asset_by_objref)(
                            input_audio_stream_objref, self.scope.get("user", None), self._session_id
                        )
                        self._input_audio_stream = AssetStream(input_audio_stream)
                    elif "agent_output_audio_stream__0" in deltas:
                        self._output_audio_task = run_coro_in_new_loop(
                            self._process_output_audio_stream(
                                deltas["agent_output_audio_stream__0"][1:]
                            )  # Remove + prefix since this is a delta
                        )
                    elif "agent_input_audio_stream_started_at" in deltas:
                        # Clear current media buffer
                        await self.send(text_data=json.dumps({"event": "clear", "streamSid": self._stream_sid}))
        elif event == "stop":
            await self._app_runner.stop()
            self._app_runner = None
        elif event == "media":
            encoded_audio_chunk = json_data.get("media", {}).get("payload", "")
            if not encoded_audio_chunk or not self._input_audio_stream:
                return

            try:
                # Decode base64 to get g711 ulaw data
                ulaw_data = base64.b64decode(encoded_audio_chunk)

                # Convert ulaw to PCM16 (lin)
                pcm_data = audioop.ulaw2lin(ulaw_data, 2)  # 2 bytes per sample for 16-bit

                # Upsample from 8kHz to 24kHz
                pcm_upsampled = audioop.ratecv(pcm_data, 2, 1, 8000, 24000, None)[0]

                # Append the converted and upsampled data
                self._input_audio_stream.append_chunk(pcm_upsampled)

            except Exception as e:
                logger.exception(f"Error converting audio format: {e}")

        elif event == "mark":
            logger.info(f"Received mark event from twilio: {json_data}")

    async def _process_output_audio_stream(self, audio_stream):
        from llmstack.assets.stream import AssetStream

        output_audio_asset = await sync_to_async(get_asset_by_objref)(
            audio_stream, self.scope.get("user", None), self._session_id
        )
        output_audio_asset_stream = AssetStream(output_audio_asset)
        async for chunk in output_audio_asset_stream.read_async(start_index=0):
            if not chunk:
                break

            audio_delta = {
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": base64.b64encode(chunk).decode("utf-8")},
            }

            await self.send(text_data=json.dumps(audio_delta))
