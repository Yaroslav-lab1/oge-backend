"""Generate deterministic, synthetic KIM ZIP fixtures for integration tests and demo."""

import hashlib
import io
import json
import wave
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def wav_silence() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 800)
    return buffer.getvalue()


def files_for(subject: str) -> dict[str, bytes]:
    math = subject == "MATH"
    schema = "MATH-OGE-SCHEMA-1.0" if math else "PHYS-OGE-SCHEMA-1.0"
    name = "Synthetic mathematics KIM" if math else "Synthetic physics KIM"
    code = "M-GEO-01" if math else "P-NUM-01"
    topic = "GEOMETRY" if math else "MECHANICS"
    task = {
        "code": code,
        "topicCode": topic,
        "answerType": "NUMBER",
        "body": {"text": "Вычислите значение по условию учебного примера."},
        "maxScore": 2,
        "geometry": math,
        "normalization": {"decimalComma": True, "allowTrailingZeros": True},
    }
    key = {"value": "7"} if math else {"value": "12.5", "unit": "м/с"}
    materials: list[dict] = []
    resources: dict[str, bytes] = {}
    if not math:
        path = "resources/instruction.wav"
        task["audioResource"] = path
        materials.append({"path": path, "mediaType": "audio/wav"})
        resources[path] = wav_silence()
    files: dict[str, bytes] = {
        "passport.json": encode(
            {"subject": subject, "name": name, "schemaVersion": schema, "sourceVersion": "1.0"}
        ),
        "topics.json": encode([{"code": topic, "title": "Учебная тема"}]),
        "plan.json": encode({"tasks": [code]}),
        "tree.json": encode({"root": topic}),
        "tasks.json": encode([task]),
        "prototypes.json": encode([{"code": f"{code}-PROTO", "taskCode": code}]),
        "keys.json": encode({code: key}),
        "criteria.json": encode({}),
        "instruction.md": "# Учебный КИМ\nВсе данные синтетические.\n".encode(),
        "materials.json": encode(materials),
        "variants/variant-01.json": encode({"code": "V1", "taskCodes": [code]}),
    }
    files.update(resources)
    return files


def pack(
    files: dict[str, bytes], *, corrupt_checksum: bool = False, extra_path: str | None = None
) -> bytes:
    passport = json.loads(files["passport.json"])
    manifest = {
        "schemaVersion": passport["schemaVersion"],
        "sourceVersion": passport["sourceVersion"],
        "files": {
            path: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for path, content in sorted(files.items())
        },
    }
    if corrupt_checksum:
        manifest["files"]["tasks.json"]["sha256"] = "0" * 64
    payload = dict(files)
    payload["manifest.json"] = encode(manifest)
    if extra_path:
        payload[extra_path] = b"forbidden"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("resources/", b"")
        archive.writestr("variants/", b"")
        for path, content in sorted(payload.items()):
            info = zipfile.ZipInfo(path, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return buffer.getvalue()


def main() -> None:
    math = files_for("MATH")
    (ROOT / "math-valid.zip").write_bytes(pack(math))
    (ROOT / "physics-valid.zip").write_bytes(pack(files_for("PHYS")))
    (ROOT / "invalid-checksum.zip").write_bytes(pack(math, corrupt_checksum=True))
    (ROOT / "invalid-traversal.zip").write_bytes(pack(math, extra_path="../escape.py"))


if __name__ == "__main__":
    main()
