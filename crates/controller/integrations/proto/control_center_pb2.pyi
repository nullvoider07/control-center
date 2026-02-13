from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class OSType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    WINDOWS: _ClassVar[OSType]
    MACOS: _ClassVar[OSType]
    LINUX: _ClassVar[OSType]
WINDOWS: OSType
MACOS: OSType
LINUX: OSType

class AgentInfoRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class AgentInfo(_message.Message):
    __slots__ = ("os", "os_version", "capabilities", "agent_version")
    OS_FIELD_NUMBER: _ClassVar[int]
    OS_VERSION_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    AGENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    os: OSType
    os_version: str
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    agent_version: str
    def __init__(self, os: _Optional[_Union[OSType, str]] = ..., os_version: _Optional[str] = ..., capabilities: _Optional[_Iterable[str]] = ..., agent_version: _Optional[str] = ...) -> None: ...

class CommandRequest(_message.Message):
    __slots__ = ("id", "command")
    ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    id: str
    command: str
    def __init__(self, id: _Optional[str] = ..., command: _Optional[str] = ...) -> None: ...

class CommandResponse(_message.Message):
    __slots__ = ("id", "success", "message", "execution_time_ms", "mouse_x", "mouse_y", "position_captured")
    ID_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    MOUSE_X_FIELD_NUMBER: _ClassVar[int]
    MOUSE_Y_FIELD_NUMBER: _ClassVar[int]
    POSITION_CAPTURED_FIELD_NUMBER: _ClassVar[int]
    id: str
    success: bool
    message: str
    execution_time_ms: int
    mouse_x: int
    mouse_y: int
    position_captured: bool
    def __init__(self, id: _Optional[str] = ..., success: bool = ..., message: _Optional[str] = ..., execution_time_ms: _Optional[int] = ..., mouse_x: _Optional[int] = ..., mouse_y: _Optional[int] = ..., position_captured: bool = ...) -> None: ...

class MonitorRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ConnectionStatus(_message.Message):
    __slots__ = ("connected", "message", "timestamp")
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    connected: bool
    message: str
    timestamp: int
    def __init__(self, connected: bool = ..., message: _Optional[str] = ..., timestamp: _Optional[int] = ...) -> None: ...

class InfoRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ExecuteRequest(_message.Message):
    __slots__ = ("id", "command", "user_id")
    ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    command: str
    user_id: str
    def __init__(self, id: _Optional[str] = ..., command: _Optional[str] = ..., user_id: _Optional[str] = ...) -> None: ...

class ExecuteResponse(_message.Message):
    __slots__ = ("id", "success", "message", "execution_time_ms", "mouse_x", "mouse_y", "position_captured")
    ID_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    MOUSE_X_FIELD_NUMBER: _ClassVar[int]
    MOUSE_Y_FIELD_NUMBER: _ClassVar[int]
    POSITION_CAPTURED_FIELD_NUMBER: _ClassVar[int]
    id: str
    success: bool
    message: str
    execution_time_ms: int
    mouse_x: int
    mouse_y: int
    position_captured: bool
    def __init__(self, id: _Optional[str] = ..., success: bool = ..., message: _Optional[str] = ..., execution_time_ms: _Optional[int] = ..., mouse_x: _Optional[int] = ..., mouse_y: _Optional[int] = ..., position_captured: bool = ...) -> None: ...

class PingRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class PongResponse(_message.Message):
    __slots__ = ("alive",)
    ALIVE_FIELD_NUMBER: _ClassVar[int]
    alive: bool
    def __init__(self, alive: bool = ...) -> None: ...
