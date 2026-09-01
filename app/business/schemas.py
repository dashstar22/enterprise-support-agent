"""Public mock-business request and response schemas. / 模拟业务接口的公开请求与响应结构。"""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas import DeviceModel, FaultCode, ShortText


class BusinessSchema(BaseModel):
    """Reject undocumented business fields. / 拒绝未记录的业务字段。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class MockDeviceResponse(BusinessSchema):
    """One device record exposed by the local mock service. / 本地模拟服务公开的一条设备记录。"""

    model: DeviceModel
    firmware_version: ShortText


class MockFaultResponse(BusinessSchema):
    """Known-fault context that must match the requested identity. / 必须与请求身份匹配的已知故障上下文。"""

    model: DeviceModel
    code: FaultCode
    known: bool


class MockInventoryResponse(BusinessSchema):
    """Minimal inventory fact required by the support workflow. / 售后流程所需的最小库存事实。"""

    model: DeviceModel
    spare_part_available: bool


class MockTicketRequest(BusinessSchema):
    """Data accepted when creating a simulated human-handoff ticket. / 创建模拟人工转接工单时接收的数据。"""

    model: DeviceModel
    fault_code: FaultCode
    summary: ShortText


class MockTicketResponse(BusinessSchema):
    """Non-production ticket identity returned by the mock service. / 模拟服务返回的非生产工单编号。"""

    ticket_id: UUID = Field(default_factory=uuid4)
    status: Literal["created"] = "created"
