#!/usr/bin/env python3

import subprocess

import os, re, json
from threading import Thread

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
    p["fps"] = project.fps
    p["jobs"] = len(project.jobs)
    p["total_jobs"] = project.total_jobs
    p["status"] = project.status
    p["encoder_params"] = project.encoder_params
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

    print("sent", new_job.projectid, new_job.scene, "to", workerid, new_job.frames)

    resp = make_response(send_file(new_job.path))
    resp.headers["success"] = "1"
    resp.headers["projectid"] = new_job.projectid
    resp.headers["filename"] = new_job.filename
    resp.headers["scene"] = new_job.scene
    resp.headers["id"] = workerid
    resp.headers["encoder"] = new_job.encoder
    resp.headers["encoder_params"] = new_job.encoder_params
    resp.headers["start"] = new_job.start
    resp.headers["frames"] = new_job.frames
    return resp
  else:
    resp = make_response("")
    resp.headers["success"] = "0"
    return resp

@app.route("/finish_job", methods=["POST"])
def receive():
  sender = request.form["id"]
  encoder = request.form["encoder"]
  encoder_params = request.form["encoder_params"]
  projectid = str(request.form["projectid"])
  scene_number = str(request.form["scene"])
  file = request.files["file"]

  if projectid not in projects:
    return "project not found", 200

  project = projects[projectid]

  if scene_number not in project.jobs:
    return "job not found", 200

  job = project.jobs[scene_number]
  scene = project.scenes[scene_number]

  if job.encoder_params != encoder_params:
    if sender in job.workers:
      job.workers.remove(sender)
    print("discard", projectid, scene_number, "bad params")
    return "bad params", 200

  encoded = os.path.join(project.path_encode, job.encoded_filename)

  if scene["filesize"] > 0:
    print("discard", projectid, scene_number, "already done")
    return "already done", 200

  os.makedirs(project.path_encode, exist_ok=True)
  file.save(encoded)
  
  if os.stat(encoded).st_size == 0: return "bad", 200
  
  dav1d = subprocess.run([
    "dav1d",
    "-i", encoded,
    "-o", "/dev/null",
    "--framethreads", "1",
    "--tilethreads", "16"
  ], capture_output=True)

  if dav1d.returncode == 1:
    print("discard", projectid, scene_number, "dav1d decode error")
    return "bad encode", 200
  
  encoded_frames = int(re.search(r"Decoded [0-9]+/([0-9]+) frames", dav1d.stdout.decode("utf-8") + dav1d.stderr.decode("utf-8")).group(1))

  if scene["frames"] != encoded_frames:
    os.remove(encoded)
    if sender in job.workers:
      job.workers.remove(sender)
    print("discard", projectid, scene_number, "frame mismatch", encoded_frames, scene["frames"])
    return "bad framecount", 200

  scene["filesize"] = os.stat(encoded).st_size

  if sender in job.workers:
    project.encoded_frames += scene["frames"]
    
  del project.jobs[scene_number]

  print("recv", projectid, scene_number, "from", sender)

  project.update_progress()
  projects.save_projects()

  if len(project.jobs) == 0 and project.get_frames() == project.total_frames:
    print("done", projectid)
    projects.add_action(project.complete)
    
  return "saved", 200

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
    project.priority = int(changes["priority"])

  return json.dumps({"success": True})

@app.route("/api/add_project", methods=["POST"])
@cross_origin()
def add_project():
  content = request.json

  for input_file in content["input"]:
    if not os.path.isfile(input_file): continue

    projects.add(Project(
      input_file,
      path_out, path_split, path_encode,
      content["encoder"],
      content["encoder_params"],
      content["min_frames"]
      ))

  return json.dumps({"success": True})

if __name__ == "__main__":
  import argparse

  parser = argparse.ArgumentParser()
  parser.add_argument("--port", dest="port", default=7899)
  args = parser.parse_args()

  # TODO: logger and curses

  projects = Projects()
  projects.load_projects(path_out, path_split, path_encode)

  print("listening on port", args.port)
  WSGIServer(app, port=args.port).start()
