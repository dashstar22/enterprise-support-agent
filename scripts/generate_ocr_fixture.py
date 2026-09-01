"""Generate the deterministic synthetic OCR panel fixture used by C4-T01."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

OUTPUT_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "fixtures" / "e200-synthetic-control-panel.png"
)


def build_image() -> bytes:
    """Create a legible panel image containing only synthetic equipment fields. / 创建只含合成设备字段的清晰面板图片。"""

    image = np.full((700, 1500, 3), 245, dtype=np.uint8)
    text_color = (20, 20, 20)
    font = cv2.FONT_HERSHEY_DUPLEX
    cv2.rectangle(image, (40, 40), (1460, 660), (30, 30, 30), 8)
    cv2.putText(image, "SYNTHETIC TEST PANEL", (110, 180), font, 2.2, text_color, 4, cv2.LINE_AA)
    cv2.putText(image, "MODEL: E-200", (110, 320), font, 2.0, text_color, 4, cv2.LINE_AA)
    cv2.putText(image, "FAULT: E01", (110, 430), font, 2.0, text_color, 4, cv2.LINE_AA)
    cv2.putText(image, "FIRMWARE: 3.1.4", (110, 540), font, 2.0, text_color, 4, cv2.LINE_AA)
    # This built-in vector font avoids an operating-system font dependency.
    # 这个内置的矢量字体不依赖操作系统是否安装某个字体文件。
    success, output = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    if not success:
        raise RuntimeError("无法编码 OCR 合成 PNG 图片")
    return output.tobytes()


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(build_image())


if __name__ == "__main__":
    main()
