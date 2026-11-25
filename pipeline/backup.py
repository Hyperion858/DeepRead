"""Backup & restore: zip course data (everything except media files) and
restore from such an archive. Media files are listed in a manifest so a
restored deployment can prompt for re-upload and match by name."""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COURSE_FILES = ("meta.json", "source.json", "lesson.json", "progress.json",
                "calibration.json", "embedded.srt")

MEDIA_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def create_backup(data_dir: Path, backups_dir: Path, note: str = "") -> Path:
    """Zip config + all course data into backups_dir. Returns archive path."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = backups_dir / f"deepread-backup-{ts}.zip"
    media_manifest = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        config = data_dir / "config.json"
        if config.exists():
            zf.write(config, "config.json")
        for cdir in sorted((data_dir / "courses").iterdir()):
            if not cdir.is_dir():
                continue
            for name in COURSE_FILES:
                f = cdir / name
                if f.exists():
                    zf.write(f, f"courses/{cdir.name}/{name}")
            # manifest: media file name + size so restore can match re-uploads
            meta_path = cdir / "meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    mp = Path(meta.get("media_path", ""))
                    media_manifest.append({
                        "course_id": cdir.name,
                        "title": meta.get("title", ""),
                        "media_file": mp.name if mp else "",
                        "media_size": mp.stat().st_size if mp.exists() else 0,
                        "kind": meta.get("kind", ""),
                    })
                except Exception:
                    pass
        zf.writestr("media_manifest.json", json.dumps(media_manifest, ensure_ascii=False, indent=2))
        info = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "note": note,
            "app": "DeepRead",
            "version": 1,
        }
        zf.writestr("backup_info.json", json.dumps(info, ensure_ascii=False, indent=2))
    return out


def list_backups(backups_dir: Path) -> list[dict[str, Any]]:
    backups_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for f in sorted(backups_dir.glob("deepread-backup-*.zip"), reverse=True):
        try:
            with zipfile.ZipFile(f) as zf:
                info = json.loads(zf.read("backup_info.json"))
            size_mb = f.stat().st_size / 1024 / 1024
            items.append({
                "file": f.name,
                "created_at": info.get("created_at", ""),
                "note": info.get("note", ""),
                "size_mb": round(size_mb, 1),
                "auto": bool(info.get("note") == "auto"),
            })
        except Exception:
            items.append({"file": f.name, "created_at": "", "note": "(损坏的备份)", "size_mb": 0, "auto": False})
    return items


def restore_backup(data_dir: Path, archive: Path) -> dict[str, Any]:
    """Restore config + course JSONs from archive. Media files are NOT restored
    (they aren't in the archive); courses come back with status 'ready' but the
    player needs the media re-uploaded into data/media with the same filename."""
    import shutil

    result = {"courses": 0, "config": False, "missing_media": []}
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        if "backup_info.json" not in names:
            raise ValueError("不是有效的 DeepRead 备份文件")
        # config
        if "config.json" in names:
            tmp = data_dir / "config.json.tmp"
            tmp.write_bytes(zf.read("config.json"))
            tmp.replace(data_dir / "config.json")
            result["config"] = True
        # courses
        course_dirs = set()
        for n in names:
            if n.startswith("courses/") and n.endswith(".json"):
                course_dirs.add(n.split("/")[1])
        for cid in course_dirs:
            cdir = data_dir / "courses" / cid
            cdir.mkdir(parents=True, exist_ok=True)
            for name in COURSE_FILES:
                arc = f"courses/{cid}/{name}"
                if arc in names:
                    (cdir / name).write_bytes(zf.read(arc))
            result["courses"] += 1
            # check media presence
            meta_path = cdir / "meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    mp = Path(meta.get("media_path", ""))
                    if not mp.exists():
                        result["missing_media"].append({
                            "course_id": cid,
                            "title": meta.get("title", ""),
                            "media_file": mp.name if mp else "",
                        })
                except Exception:
                    pass
        # media manifest for reference
        if "media_manifest.json" in names:
            (data_dir / "backups" / "last_restore_manifest.json").write_bytes(zf.read("media_manifest.json"))
    return result


def attach_media(cid: str, data_dir: Path, media_file: Path) -> dict[str, Any]:
    """Re-link a re-uploaded media file to a restored course (match by name)."""
    cdir = data_dir / "courses" / cid
    meta_path = cdir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError("课程不存在")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    expected = Path(meta.get("media_path", "")).name
    if media_file.name != expected:
        return {"ok": False, "detail": f"文件名不匹配：需要 {expected}"}
    dest = data_dir / "media" / media_file.name
    shutil.copyfile(media_file, dest)
    meta["media_path"] = str(dest)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}
