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
MATERIAL_FILES = ("material.json", "embedded.srt")

MEDIA_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def create_backup(data_dir: Path, backups_dir: Path, note: str = "",
                  include_media: bool = True) -> Path:
    """Zip config + course data (+ optionally media files) into backups_dir."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    out = backups_dir / f"deepread-backup-{ts}.zip"
    media_manifest = []
    archived_media: set[str] = set()  # dedupe: same media file only archived once
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        config = data_dir / "config.json"
        if config.exists():
            zf.write(config, "config.json")
        # courses
        for cdir in sorted((data_dir / "courses").iterdir()):
            if not cdir.is_dir():
                continue
            for name in COURSE_FILES:
                f = cdir / name
                if f.exists():
                    zf.write(f, f"courses/{cdir.name}/{name}")
        # materials (raw media library)
        materials_root = data_dir / "materials"
        if materials_root.exists():
            for mdir in sorted(materials_root.iterdir()):
                if not mdir.is_dir():
                    continue
                for name in MATERIAL_FILES:
                    f = mdir / name
                    if f.exists():
                        zf.write(f, f"materials/{mdir.name}/{name}")
                mat_path = mdir / "material.json"
                if mat_path.exists():
                    try:
                        mat = json.loads(mat_path.read_text(encoding="utf-8"))
                        mp = Path(mat.get("media_path", ""))
                        media_manifest.append({
                            "kind": "material",
                            "id": mdir.name,
                            "title": mat.get("title", ""),
                            "media_file": mp.name if mp else "",
                            "media_size": mp.stat().st_size if mp.exists() else 0,
                            "in_archive": bool(include_media and mp.exists()),
                        })
                        if include_media and mp.exists():
                            zf.write(mp, f"media/{mp.name}")
                            archived_media.add(mp.name)
                    except Exception:
                        pass
        # legacy media dir — only files NOT already archived via a material
        # (migrated materials keep media_path pointing into data/media)
        media_dir = data_dir / "media"
        if media_dir.exists():
            for mp in sorted(media_dir.iterdir()):
                if mp.suffix.lower() not in MEDIA_EXTS:
                    continue
                dup = mp.name in archived_media
                media_manifest.append({
                    "kind": "legacy_media",
                    "id": mp.stem,
                    "title": "",
                    "media_file": mp.name,
                    "media_size": mp.stat().st_size,
                    "in_archive": bool(include_media) and not dup,
                    "duplicate_of_material": dup,
                })
                if include_media and not dup:
                    zf.write(mp, f"media/{mp.name}")
                    archived_media.add(mp.name)
        zf.writestr("media_manifest.json", json.dumps(media_manifest, ensure_ascii=False, indent=2))
        info = {
            "created_at": datetime.now().astimezone().isoformat(),
            "note": note,
            "include_media": bool(include_media),
            "app": "DeepRead",
            "version": 2,
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
                "has_media": bool(info.get("include_media")),
            })
        except Exception:
            items.append({"file": f.name, "created_at": "", "note": "(损坏的备份)",
                          "size_mb": 0, "auto": False, "has_media": False})
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
        # materials
        material_dirs = set()
        for n in names:
            if n.startswith("materials/") and n.endswith(".json"):
                material_dirs.add(n.split("/")[1])
        for mid in material_dirs:
            mdir = data_dir / "materials" / mid
            mdir.mkdir(parents=True, exist_ok=True)
            for name in MATERIAL_FILES:
                arc = f"materials/{mid}/{name}"
                if arc in names:
                    (mdir / name).write_bytes(zf.read(arc))
        result["materials"] = len(material_dirs)
        # media files (present when the archive was created with media included)
        media_files = [n for n in names if n.startswith("media/") and len(n) > len("media/")]
        restored_names = set()
        for mf in media_files:
            fname = Path(mf).name
            dest = data_dir / "media" / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(zf.read(mf))
            tmp.replace(dest)
            restored_names.add(fname)
            result["media_restored"] = result.get("media_restored", 0) + 1
        # re-link restored media into material dirs by filename
        for mid in material_dirs:
            mat_path = data_dir / "materials" / mid / "material.json"
            if not mat_path.exists():
                continue
            try:
                mat = json.loads(mat_path.read_text(encoding="utf-8"))
                expected = Path(mat.get("media_path", "")).name
                if expected in restored_names:
                    mat["media_path"] = str(data_dir / "media" / expected)
                    mat_path.write_text(json.dumps(mat, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        # check media presence for every course + material
        for cid in course_dirs:
            meta_path = data_dir / "courses" / cid / "meta.json"
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
    """Re-link a re-uploaded media file to a restored course or material.

    Tries course data/courses/<cid>/meta.json first, then material
    data/materials/<cid>/material.json — matched by expected filename."""
    for kind, rel in (("course", "courses"), ("material", "materials")):
        info_path = data_dir / rel / cid / ("meta.json" if kind == "course" else "material.json")
        if not info_path.exists():
            continue
        info = json.loads(info_path.read_text(encoding="utf-8"))
        expected = Path(info.get("media_path", "")).name
        if media_file.name != expected:
            return {"ok": False, "detail": f"文件名不匹配：需要 {expected}"}
        dest = data_dir / "media" / media_file.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(media_file, dest)
        info["media_path"] = str(dest)
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True}
    raise FileNotFoundError("课程或素材不存在")
