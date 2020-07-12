#!/usr/bin/env python3

import subprocess

import os, re, json
from threading import Thread

from logger import Logger
from project import Projects, Project

from flask import Flask, request, send_file, make_response, send_from_directory
from flask_cors import cross_origin
from wsgiserver import WSGIServer

path_split = "jobs/{}/split"
path_encode = "jobs/{}/encode"
path_out = "jobs/{}/completed.webm"

app = Flask(__name__)

@app.route("/scene/<projectid>/<scene>", methods=["GET"])
@cross_origin()
def get_scene(projectid, scene):
  if projectid in projects:
    return send_from_directory(projects[projectid].path_encode, scene)
  return "", 404

@app.route("/completed/<projectid>", methods=["GET"])
@cross_origin()
def get_completed(projectid):
  if projectid in projects:
    return send_file(projects[projectid].path_out)
  return "", 404

@app.route("/api/get_projects", methods=["GET"])
@cross_origin()
def get_projects():
  rtn = []
  for project in projects.values():
    p = {}
    p["projectid"] = project.projectid
    p["input"] = project.path_in
    p["frames"] = project.get_frames()
    p["total_frames"] = project.input_total_frames
    p["jobs"] = len(project.jobs)
    p["total_jobs"] = project.total_jobs
    p["status"] = project.status
    p["encoder_params"] = project.encoder_params
    p["ffmpeg_params"] = project.ffmpeg_params
    p["encoder"] = project.encoder
    p["scenes"] = project.scenes
    p["priority"] = project.priority
    p["workers"] = [job for job in project.jobs if len(project.jobs[job].workers) > 0]

    rtn.append(p)
  return json.dumps(rtn)

@app.route("/api/get_job/<jobs>", methods=["GET"])
def get_job(jobs):
  jobs = json.loads(jobs)

  new_job = projects.get_job(jobs)

  if new_job:
    workerid = f"{request.environ['REMOTE_ADDR']}:{request.environ['REMOTE_PORT']}"
    new_job.workers.append(workerid)

    logger.add("net", "sent", new_job.project.projectid, new_job.scene, "to", workerid, new_job.frames)

    resp = make_response(send_file(new_job.path))
    resp.headers["success"] = "1"
    resp.headers["projectid"] = new_job.project.projectid
    resp.headers["filename"] = new_job.filename
    resp.headers["scene"] = new_job.scene
    resp.headers["id"] = workerid
    resp.headers["encoder"] = new_job.encoder
    resp.headers["encoder_params"] = new_job.encoder_params
    resp.headers["ffmpeg_params"] = new_job.ffmpeg_params
    resp.headers["version"] = versions[new_job.encoder]
    resp.headers["start"] = new_job.start
    resp.headers["frames"] = new_job.frames
    return resp
  else:
    resp = make_response("")
    resp.headers["success"] = "0"
    return resp

@app.route("/cancel_job", methods=["POST"])
def cancel_job():
  client = request.form["client"] if "client" in request.form else request.form["id"]
  projectid = str(request.form["projectid"])
  scene_number = str(request.form["scene"])

  if projectid not in projects:
    return "project not found", 200

  project = projects[projectid]

  if scene_number not in project.jobs:
    return "job not found", 200

  job = project.jobs[scene_number]

  if client in job.workers:
    job.workers.remove(client)
    logger.add("net", "cancel", projectid, scene_number, "by", client)

  return "saved", 200

@app.route("/finish_job", methods=["POST"])
def receive():
  client = request.form["client"]
  encoder = request.form["encoder"]
  version = request.form["version"]

  if version != versions[encoder]:
    return "bad encoder version", 200

  encoder_params = request.form["encoder_params"]
  ffmpeg_params = request.form["ffmpeg_params"]
  projectid = str(request.form["projectid"])
  scene_number = str(request.form["scene"])
  file = request.files["file"]

  return projects.check_job(projectid, client, encoder, encoder_params, ffmpeg_params, scene_number, file), 200

@app.route("/api/list_directory", methods=["GET"])
@cross_origin()
def list_directory():
  return json.dumps(os.listdir("inputfiles"))

@app.route("/api/delete_project/<projectid>", methods=["POST"])
@cross_origin()
def delete_project(projectid):
  if projectid not in projects:
    return json.dumps({"success": False, "reason": "Project does not exist."})

  projects[projectid].stopped = True
  del projects[projectid]

  return json.dumps({"success": True})

@app.route("/api/modify/<projectid>", methods=["POST"])
@cross_origin()
def modify_project(projectid):
  if projectid not in projects:
    return json.dumps({"success": False, "reason": "Project does not exist."})

  project = projects[projectid]

  changes = request.json

  if "priority" in changes:
    if not isinstance(changes["priority"], (int, float)):
      return json.dumps({
        "success": False,
        "reason": "priority must be a number"
      })
    project.priority = changes["priority"]

  if "on_complete" in changes:
    project.action = changes["on_complete"]

  return json.dumps({"success": True})

@app.route("/api/add_project", methods=["POST"])
@cross_origin()
def add_project():
  content = request.json

  missing_fields = ",".join([key for key in ["input", "encoder", "encoder_params"] if key not in content])
  if missing_fields:
    return json.dumps({"success": False, "reason": f"Missing fields {missing_fields}"})

  if not (isinstance(content["min_frames"], int) and \
    isinstance(content["max_frames"], int)):
    return json.dumps({
      "success": False,
      "reason": "min_frames and max_frames must be of type integer"
    })

  if not isinstance(content["priority"], (int, float)):
    return json.dumps({
      "success": False,
      "reason": "priority must be a number"
    })
  
  if not content["input"]:
    return json.dumps({"success": False, "reason": "input is empty"})

  missing_files = ",".join([f for f in content["input"] if not os.path.isfile(f)])
  if missing_files:
    return json.dumps({"success": False, "reason": f"Input files not found: {missing_files}"})
  
  for input_file in content["input"]:
    logger.add("net", "add project", input_file)

    projects.add(Project(
      input_file,
      path_out,
      path_split,
      path_encode, 
      content["encoder"],
      content["encoder_params"],
      ffmpeg_params=content["ffmpeg_params"] if "ffmpeg_params" in content else "",
      min_frames=content["min_frames"] if "min_frames" in content else -1,
      max_frames=content["max_frames"] if "max_frames" in content else -1,
      priority=content["priority"] if "priority" in content else 0
    ), content["on_complete"] if "on_complete" in content else "")

  return json.dumps({"success": True})

@app.route("/api/get_info", methods=["GET"])
@cross_origin()
def get_info():
  info = {
    "versions": {
      "libaom": versions["aom"],
      "libvpx": versions["vpx"],
      "dav1d": versions["dav1d"]
    },
    "projects": len(projects),
    "jobs": len([job for pid in projects.projects for job in projects[pid].jobs]),
    "frames per hour": {
      "since": projects.telemetry["fph_time"],
      "frames": projects.telemetry["fph"]
    },
    "userstats": projects.userstats
  }
  return json.dumps(info)

def get_dav1d_version():
  p = subprocess.run(["dav1d", "-v"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  return p.stdout.decode("utf-8").strip() + p.stderr.decode("utf-8").strip()

def get_aomenc_version():
  p = subprocess.run(["aomenc", "--help"], stdout=subprocess.PIPE)
  r = re.search(r"av1\s+-\s+(.+)\n", p.stdout.decode("utf-8"))
  return r.group(1).replace("(default)", "").strip()

def get_vpxenc_version():
  p = subprocess.run(["vpxenc", "--help"], stdout=subprocess.PIPE)
  r = re.search(r"vp9\s+-\s+(.+)\n", p.stdout.decode("utf-8"))
  return r.group(1).replace("(default)", "").strip()

if __name__ == "__main__":
  import argparse

  parser = argparse.ArgumentParser()
  parser.add_argument("--port", dest="port", default=7899)
  args = parser.parse_args()

  versions = {
    "aom": get_aomenc_version(),
    "vpx": get_vpxenc_version(),
    "dav1d": get_dav1d_version()
  }

  logger = Logger()

  from util import vs_core

  if vs_core:
    logger.add("info", "Vapoursynth supported")

  projects = Projects(logger)
  projects.load_projects(path_out, path_split, path_encode)

  logger.add("default", "listening on port", args.port)
  WSGIServer(app, port=args.port).start()
