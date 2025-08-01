import json
import logging
import time
from datetime import datetime, timezone
from http import HTTPStatus
from logging import Logger
from typing import List, Union

import click
import uvicorn
from fastapi import FastAPI, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRouter
from pydantic import AnyHttpUrl

from isar.apis.models.models import ControlMissionResponse, StartMissionResponse
from isar.apis.robot_control.robot_controller import RobotController
from isar.apis.schedule.scheduling_controller import SchedulingController
from isar.apis.security.authentication import Authenticator
from isar.config.keyvault.keyvault_service import Keyvault
from isar.config.settings import settings
from robot_interface.telemetry.mqtt_client import MqttClientInterface
from robot_interface.telemetry.payloads import StartUpMessagePayload
from robot_interface.utilities.json_service import EnhancedJSONEncoder


class API:
    def __init__(
        self,
        authenticator: Authenticator,
        scheduling_controller: SchedulingController,
        robot_controller: RobotController,
        keyvault: Keyvault,
        mqtt_publisher: MqttClientInterface,
        port: int = settings.API_PORT,
    ) -> None:
        self.authenticator: Authenticator = authenticator
        self.scheduling_controller: SchedulingController = scheduling_controller
        self.robot_controller: RobotController = robot_controller
        self.keyvault: Keyvault = keyvault
        self.host: str = "0.0.0.0"  # Locking uvicorn to use 0.0.0.0
        self.port: int = port
        self.mqtt_publisher: MqttClientInterface = mqtt_publisher

        self.logger: Logger = logging.getLogger("api")

        self.app: FastAPI = self._create_app()
        self.server = self._setup_server()

    def get_app(self) -> FastAPI:
        return self.app

    def _setup_server(self) -> uvicorn.Server:
        config = uvicorn.Config(
            self.app,
            port=self.port,
            host=self.host,
            reload=False,
            log_config=None,
        )
        return uvicorn.Server(config)

    def wait_for_api_server_ready(self) -> None:
        while not self.server.started:
            time.sleep(0.01)
        self.logger.info("Uvicorn server has been started")
        self._publish_startup_message()

    def _create_app(self) -> FastAPI:
        tags_metadata = [
            {
                "name": "Scheduler",
                "description": "Mission functionality",
            }
        ]
        app = FastAPI(
            openapi_tags=tags_metadata,
            on_startup=[
                self.authenticator.load_config,
                self._log_startup_message,
            ],
            swagger_ui_oauth2_redirect_url="/oauth2-redirect",
            swagger_ui_init_oauth={
                "usePkceWithAuthorizationCodeGrant": True,
                "clientId": settings.AZURE_CLIENT_ID,
            },
        )

        if self.authenticator.should_authenticate():
            backend_cors_origins: List[Union[str, AnyHttpUrl]] = [
                f"http://{self.host}:{self.port}"
            ]

            app.add_middleware(
                CORSMiddleware,
                allow_origins=[str(origin) for origin in backend_cors_origins],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

        app.include_router(router=self._create_scheduler_router())

        app.include_router(router=self._create_info_router())

        app.include_router(router=self._create_media_control_router())

        return app

    def _create_scheduler_router(self) -> APIRouter:
        router: APIRouter = APIRouter(tags=["Scheduler"])

        authentication_dependency: Security = Security(self.authenticator.get_scheme())

        router.add_api_route(
            path="/schedule/start-mission/{id}",
            endpoint=self.scheduling_controller.start_mission_by_id,
            methods=["POST"],
            deprecated=True,
            dependencies=[authentication_dependency],
            summary="Start a mission with id='id' from the current mission planner",
            responses={
                HTTPStatus.OK.value: {
                    "description": "Mission succesfully started",
                    "model": StartMissionResponse,
                },
                HTTPStatus.NOT_FOUND.value: {
                    "description": "Not found - Mission not found",
                },
                HTTPStatus.CONFLICT.value: {
                    "description": "Conflict - Invalid command in the current state",
                },
                HTTPStatus.INTERNAL_SERVER_ERROR.value: {
                    "description": "Internal Server Error - Current state of state machine unknown",
                },
            },
        )
        router.add_api_route(
            path="/schedule/start-mission",
            endpoint=self.scheduling_controller.start_mission,
            methods=["POST"],
            dependencies=[authentication_dependency],
            summary="Start the mission provided in JSON format",
            responses={
                HTTPStatus.OK.value: {
                    "description": "Mission succesfully started",
                    "model": StartMissionResponse,
                },
                HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                    "description": "Invalid body - The JSON is incorrect",
                },
                HTTPStatus.CONFLICT.value: {
                    "description": "Conflict - Invalid command in the current state",
                },
                HTTPStatus.BAD_REQUEST.value: {
                    "description": "Bad request - Robot does not have the capabilities to perform the mission",
                },
                HTTPStatus.INTERNAL_SERVER_ERROR.value: {
                    "description": "Internal Server Error - Current state of state machine unknown",
                },
            },
        )
        router.add_api_route(
            path="/schedule/return-home",
            endpoint=self.scheduling_controller.return_home,
            methods=["POST"],
            dependencies=[authentication_dependency],
            summary="Start return home mission",
            responses={
                HTTPStatus.OK.value: {
                    "description": "Return home mission succesfully started",
                    "model": StartMissionResponse,
                },
                HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                    "description": "Invalid body - The JSON is incorrect",
                },
                HTTPStatus.CONFLICT.value: {
                    "description": "Conflict - Invalid command in the current state",
                },
                HTTPStatus.INTERNAL_SERVER_ERROR.value: {
                    "description": "Internal Server Error - Current state of state machine unknown",
                },
            },
        )
        router.add_api_route(
            path="/schedule/stop-mission",
            endpoint=self.scheduling_controller.stop_mission,
            methods=["POST"],
            dependencies=[authentication_dependency],
            summary="Stop the current mission",
            responses={
                HTTPStatus.OK.value: {
                    "description": "Mission succesfully stopped",
                    "model": ControlMissionResponse,
                },
                HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                    "description": "Invalid body - The JSON is incorrect",
                },
                HTTPStatus.CONFLICT.value: {
                    "description": "Conflict - Invalid command in the current state",
                },
                HTTPStatus.BAD_REQUEST.value: {
                    "description": "Bad request - Robot does not have the capabilities to perform the mission",
                },
                HTTPStatus.INTERNAL_SERVER_ERROR.value: {
                    "description": "Internal Server Error - Current state of state machine unknown",
                },
            },
        )
        router.add_api_route(
            path="/schedule/pause-mission",
            endpoint=self.scheduling_controller.pause_mission,
            methods=["POST"],
            dependencies=[authentication_dependency],
            summary="Pause the current mission",
            responses={
                HTTPStatus.OK.value: {
                    "description": "Mission succesfully paused",
                    "model": ControlMissionResponse,
                },
                HTTPStatus.CONFLICT.value: {
                    "description": "Conflict - Invalid command in the current state",
                },
                HTTPStatus.INTERNAL_SERVER_ERROR.value: {
                    "description": "Internal Server Error - Current state of state machine unknown",
                },
            },
        )
        router.add_api_route(
            path="/schedule/resume-mission",
            endpoint=self.scheduling_controller.resume_mission,
            methods=["POST"],
            dependencies=[authentication_dependency],
            summary="Resume the currently paused mission - if any",
            responses={
                HTTPStatus.OK.value: {
                    "description": "Mission succesfully resumed",
                    "model": ControlMissionResponse,
                },
                HTTPStatus.CONFLICT.value: {
                    "description": "Conflict - Invalid command in the current state",
                },
                HTTPStatus.INTERNAL_SERVER_ERROR.value: {
                    "description": "Internal Server Error - Current state of state machine unknown",
                },
            },
        )
        router.add_api_route(
            path="/schedule/move_arm/{arm_pose_literal}",
            endpoint=self.scheduling_controller.start_move_arm_mission,
            methods=["POST"],
            dependencies=[authentication_dependency],
            summary="Move arm to the given arm pose literal",
            responses={
                HTTPStatus.OK.value: {
                    "description": "Move arm mission successfully started",
                },
                HTTPStatus.BAD_REQUEST.value: {
                    "description": "A move arm mission was scheduled on a robot that "
                    "does not support it or the input was incorrect",
                },
                HTTPStatus.CONFLICT.value: {
                    "description": "Conflict - Invalid command in the current state",
                },
                HTTPStatus.INTERNAL_SERVER_ERROR.value: {
                    "description": "Internal Server Error - Current state of state machine unknown",
                },
            },
        )

        return router

    def _create_info_router(self) -> APIRouter:
        router: APIRouter = APIRouter(tags=["Info"])

        authentication_dependency: Security = Security(self.authenticator.get_scheme())

        router.add_api_route(
            path="/info/robot-settings",
            endpoint=self.robot_controller.get_info,
            methods=["GET"],
            dependencies=[authentication_dependency],
            summary="Information about the robot-settings",
        )

        return router

    def _create_media_control_router(self) -> APIRouter:
        router: APIRouter = APIRouter(tags=["Media"])

        authentication_dependency: Security = Security(self.authenticator.get_scheme())

        router.add_api_route(
            path="/media/media-stream-config",
            endpoint=self.robot_controller.generate_media_config,
            methods=["GET"],
            dependencies=[authentication_dependency],
            summary="Generates a media stream connection config",
            responses={
                HTTPStatus.OK.value: {
                    "description": "Media stream was successfully generated",
                },
                HTTPStatus.NO_CONTENT.value: {
                    "description": "Robot has no media config",
                },
            },
        )

        return router

    def _log_startup_message(self) -> None:
        address_format = "%s://%s:%d/docs"
        message = f"Uvicorn running on {address_format} (Press CTRL+C to quit)"
        protocol = "http"
        color_message = (
            "Uvicorn running on "
            + click.style(address_format, bold=True)
            + " (Press CTRL+C to quit)"
        )
        self.logger.info(
            message,
            protocol,
            self.host,
            self.port,
            extra={"color_message": color_message},
        )

    def _publish_startup_message(self) -> None:
        if not self.mqtt_publisher:
            return

        payload: StartUpMessagePayload = StartUpMessagePayload(
            isar_id=settings.ISAR_ID,
            timestamp=datetime.now(timezone.utc),
        )

        self.logger.info("Publishing startup message to MQTT broker")

        self.mqtt_publisher.publish(
            topic=settings.TOPIC_ISAR_STARTUP,
            payload=json.dumps(payload, cls=EnhancedJSONEncoder),
            qos=1,
            retain=True,
        )
