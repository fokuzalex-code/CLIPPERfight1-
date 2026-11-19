import json, threading, time, traceback, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from . import config as cfg, downloader, highlights, tracker, transcriber, video_processor as vp

EXEC = ThreadPoolExecutor(max_workers=1)  # 1 render a la vez: FFmpeg/Whisper ya usan todos los núcleos
JOBS, LOCK = {}, threading.RLock()
DUR = {"15-30": (15, 30), "30-60": (30, 60), "60-90": (60, 90)}


def _save(job):
    (cfg.JOBS_DIR / f"{job['id']}.json").write_text(json.dumps(job))


def upd(job, **kw):
    with LOCK:
        job.update(kw)
        _save(job)


def load_jobs():
    cfg.ensure()
    for f in cfg.JOBS_DIR.glob("*.json"):
        try:
            j = json.loads(f.read_text())
            if j["status"] not in ("done", "error"):
                j["status"], j["message"] = "error", "Interrumpido por reinicio del servidor"
            for c in j["clips"]:
                if c["status"] in ("queued", "rendering"):
                    c["status"], c["error"] = "error", "Interrumpido"
            JOBS[j["id"]] = j
        except Exception:
            pass


def public(job):
    return {k: v for k, v in job.items() if k not in ("cands", "used", "work")}


def busy():
    return any(j["status"] not in ("done", "error") or any(c["status"] in ("queued", "rendering") for c in j["clips"])
               for j in JOBS.values())


def new_job(req):
    jid = uuid.uuid4().hex[:10]
    od, wd = cfg.out_dir() / jid, cfg.WORK / jid
    od.mkdir(parents=True, exist_ok=True)
    wd.mkdir(parents=True, exist_ok=True)
    job = {"id": jid, "status": "queued", "progress": 0, "message": "En cola…", "req": req,
           "dir": str(od), "work": str(wd), "clips": [], "cands": [], "used": [],
           "created": time.time(), "info": None}
    with LOCK:
        JOBS[jid] = job
        _save(job)
    EXEC.submit(run_job, jid)
    return job


def _mk_clip(i, c, req):
    return {"id": uuid.uuid4().hex[:6], "idx": i, "start": c["s"], "end": c["e"], "score": c["score"],
            "layout": req["layout"], "status": "queued", "ver": 0, "file": None, "thumb": None,
            "title": "", "desc": "", "tags": "", "name": "", "trashed": False, "crop": None,
            "error": None, "wm": {"user": req["wm_user"], "opacity": req["wm_opacity"], "pos": req["wm_pos"]}}


def run_job(jid):
    job, S = JOBS[jid], cfg.load()
    req, wd = job["req"], Path(job["work"])
    try:
        if req.get("url"):
            upd(job, status="downloading", message="Descargando de Kick…", progress=2)
            last = [0]

            def dl(p):
                v = 2 + int(p * 18)
                if v != last[0]:
                    last[0] = v
                    upd(job, progress=v)
            raw = downloader.download_kick(req["url"], wd / "dl", int(req.get("start_min") or 0),
                                           int(S["max_source_minutes"]), dl)
        else:
            files = [f for f in (cfg.UPLOADS / req["file_id"]).iterdir() if f.is_file()]
            raw = files[0]
        upd(job, status="normalizing", message="Normalizando video (H.264/AAC)…", progress=20)
        src = wd / "source.mp4"
        downloader.normalize(raw, src)
        info = downloader.probe(src)
        upd(job, info=info, status="transcribing", message="Transcribiendo con Whisper…", progress=25)
        tr = transcriber.transcribe(src, wd, S["whisper_model"], S["language"],
                                    lambda p: upd(job, progress=25 + int(p * 30)))
        upd(job, status="analyzing", message="Buscando los mejores momentos…", progress=56)
        dmin, dmax = DUR[req["duration"]]
        ranked = highlights.find_candidates(wd / "audio.wav", tr, dmin, dmax, info["duration"],
                                            int(S["sensitivity"]), int(req["count"]))
        if not ranked:
            raise RuntimeError("No se encontraron momentos válidos (video demasiado corto o sin audio)")
        thr = 20 + 0.5 * int(S["sensitivity"])  # sensibilidad: exigencia mínima de score
        count = int(req["count"])
        picks = [c for c in ranked if c["score"] >= thr][:count]
        picks += [c for c in ranked if c not in picks][:count - len(picks)]
        clips = [_mk_clip(i, c, req) for i, c in enumerate(picks)]
        upd(job, cands=ranked, used=[c["s"] for c in picks], clips=clips, status="rendering",
            message=f"Renderizando 0/{len(clips)}…", progress=60)
        for n, clip in enumerate(clips):
            render_clip(job, clip)
            upd(job, progress=60 + int(40 * (n + 1) / len(clips)), message=f"Renderizando {n + 1}/{len(clips)}…")
        upd(job, status="done", message="¡Listo!", progress=100)
    except Exception as e:
        traceback.print_exc()
        upd(job, status="error", message=str(e)[:300])


def render_clip(job, clip):
    S, req = cfg.load(), job["req"]
    wd, od = Path(job["work"]), Path(job["dir"])
    try:
        clip["status"], clip["error"] = "rendering", None
        upd(job)
        src, info = wd / "source.mp4", job["info"]
        if not src.exists():
            raise RuntimeError("La fuente ya no existe (caché borrada). Genera el video de nuevo.")
        tr = json.loads((wd / "transcript.json").read_text())
        s, e, layout = clip["start"], clip["end"], clip["layout"]
        words = [{"w": w["w"], "s": round(w["s"] - s, 2), "e": round(min(w["e"], e) - s, 2)}
                 for w in tr["words"] if s <= w["s"] < e]
        fillers = [(w["s"], w["e"]) for w in words if transcriber.is_filler(w["w"])]
        meta = highlights.describe(tr, s, e, S["hashtags"])
        faces = []
        if layout != "original":
            faces = tracker.face_plan(src, s, e)["faces"]
            if clip.get("crop"):
                c = clip["crop"]
                faces = [{"x": c["x"] / 100, "y": c["y"] / 100, "w": .2, "h": .3}]
        wm = clip["wm"]
        o = {"sub_font": S["sub_font"], "sub_color": S["sub_color"], "sub_active": S["sub_active"],
             "sub_pos": S["sub_position"], "sub_size": S["sub_size"], "wm_user": wm["user"],
             "wm_opacity": wm["opacity"], "wm_pos": wm["pos"], "hook": req["hook"], "hook_text": meta["hook"],
             "stinger": req["stinger"], "progress_bar": req["progress_bar"], "blur": req["blur"],
             "anti_fillers": bool(req["fillers"]), "target_lufs": S["target_lufs"], "punch": True}
        ver = clip["ver"] + 1
        fname = f"{clip['id']}_v{ver}.mp4"
        vp.render(src, od / fname, s, e, layout, faces, info, words, fillers, o)
        thumb = f"{clip['id']}_v{ver}.jpg"
        vp.thumbnail(od / fname, od / thumb)
        for old in (clip.get("file"), clip.get("thumb")):
            if old:
                (od / old).unlink(missing_ok=True)
        clip.update(ver=ver, file=fname, thumb=thumb, title=meta["title"], desc=meta["desc"], tags=meta["tags"],
                    name=f"{clip['idx'] + 1:02d}_{highlights.slug(meta['title'])}_{layout}_{clip['score']}.mp4",
                    status="ready")
    except Exception as ex:
        traceback.print_exc()
        clip.update(status="error", error=str(ex)[:300])
    upd(job)


def _task(jid, cid):
    job = JOBS[jid]
    clip = next(c for c in job["clips"] if c["id"] == cid)
    render_clip(job, clip)


def reroll(jid, cid):
    job = JOBS[jid]
    clip = next(c for c in job["clips"] if c["id"] == cid)
    nxt = next((c for c in job["cands"] if c["s"] not in job["used"]), None)
    if not nxt:
        raise ValueError("No quedan más candidatos en este video")
    with LOCK:
        job["used"].append(nxt["s"])
        clip.update(start=nxt["s"], end=nxt["e"], score=nxt["score"], crop=None, status="queued", trashed=False)
    upd(job)
    EXEC.submit(_task, jid, cid)


def edit(jid, cid, d):
    job = JOBS[jid]
    clip = next(c for c in job["clips"] if c["id"] == cid)
    s, e = float(d["start"]), float(d["end"])
    if not (5 <= e - s <= 180) or s < 0 or e > job["info"]["duration"] + .5:
        raise ValueError("Rango inválido (5–180 s dentro del video)")
    if d["layout"] not in ("split", "cam", "podcast", "original"):
        raise ValueError("Layout inválido")
    wm = d.get("wm") or clip["wm"]
    with LOCK:
        clip.update(start=s, end=min(e, job["info"]["duration"]), layout=d["layout"], crop=d.get("crop"),
                    wm={"user": str(wm.get("user", ""))[:40], "opacity": max(0, min(1, float(wm.get("opacity", .6)))),
                        "pos": wm.get("pos", "br")}, status="queued")
    upd(job)
    EXEC.submit(_task, jid, cid)


def trash(jid, cid):
    job = JOBS[jid]
    clip = next(c for c in job["clips"] if c["id"] == cid)
    for k in ("file", "thumb"):
        if clip.get(k):
            (Path(job["dir"]) / clip[k]).unlink(missing_ok=True)
    clip["trashed"] = True
    upd(job)
