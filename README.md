# grav1
## Distributed network av1 / vp9 encoder

[web client](https://encode.grass.moe)

message me on the av1 discord if you're actually going to use this

### Preview

![client](https://github.com/wwww-wwww/grav1/raw/master/images/client.gif)

<img src="https://github.com/wwww-wwww/grav1/raw/master/images/website.png" width="600">

### Latest significant changes:
- split algorithm is updated [here](https://github.com/wwww-wwww/grav1ty) first
- scene information is stored individually for each project

### Requirements
Server (system):
```
python3
ffmpeg
aomenc
vpxenc
dav1d
```
Server (python):
```
flask
flask-cors
wsgiserver
vapoursynth (optional)
```
Client (system):
```
python3
curses
ffmpeg
aomenc (vmaf optional)
vpxenc
```
Client (python):  
```
requests
```

### Usage

this only supports aomenc and vpxenc  
(I don't plan on supporting other encoders)

start webserver (default port: 7899)  
`python server.py`  
`python server.py --port 1234`

starting up a worker  
`python grav1c.py http://target --workers 4`  

access the server through the [web client](https://encode.grass.moe) (incomplete)

## Web ui api
If you want your program to support my web client, here are the specifications:

## Get Server Info ##

These are required to tell the webui what is available and what is not

Name: `/api/get_info`

Method: GET

**Parameters:**

None

**Returns:**

JSON object

**Example:**

```json
{
  "encoders": {
    "aomenc": "AOMedia Project AV1 Encoder 2.0.0-487-ga822b3cc6",
    "vpxenc": "WebM Project VP9 Encoder v1.8.2-209-gde4aedaec"
  },
  "actions": ["merge"],
  "protocols": ["http-get"],
  "logs": ["net", "info"],
  "password": true
}
```

## Get Server Extra Info ##

This can be a map with keys and values of any type

Name: `/api/get_home`

Method: GET

**Parameters:**

None

**Returns:**

JSON object

**Example:**

```json
{
  "encoders": {
    "libaom": "AOMedia Project AV1 Encoder 2.0.0-487-ga822b3cc6",
    "libvpx": "WebM Project VP9 Encoder v1.8.2-209-gde4aedaec"
  },
  "projects": 15,
  "some other": "information"
}
```

## Get Projects ##

Name: `/api/get_projects`

Method: GET

**Parameters:**

None

**Returns:**

JSON list of objects

**Example:**

```json
[
  {
    "projectid": "1",
    "input": "path/to/file.mkv",
    "frames": 50,
    "total_frames": 100,
    "jobs": 1,
    "total_jobs": 2,
    "status": "ready",
    "size": 12345
  },
  {
    "projectid": "4",
    ...
  }
  ...
]
```

## Get Project ##

Name: `/api/get_project/<projectid>`

Method: GET

**Parameters:**

Parameter                         | Type    | Description
----------------------------------|---------|------------
`projectid`                       | string  | Id representing the project

**Returns:**

JSON objects

**Example:**

```
{
  "projectid": "12345",
  "input": "path/to/file.mkv",
  "frames": 20,
  "total_frames": 200,
  "jobs": 2,
  "total_jobs": 10,
  "status": "ready",
  "encoder": "aom",
  "encoder_params": "-b 10 --cpu-used=3 --end-usage=q --cq-level=20",
  "ffmpeg_params": "",
  "scenes": {
    "00001": {"filesize": 427911, "frames": 50, "encoder_params": ""},
    "00002": {"filesize": 503284, "frames": 50, "encoder_params": ""}
  },
  "priority": 0,
  "workers": []
}
```

## Create Project ##

Name: `/api/add_project`

Method: POST

**Parameters:**

Requires a Json object in the body

Property                          | Type    | Description
----------------------------------|---------|------------
`input`                           | list    | List of filenames / paths
`encoder`                         | string  | Encoder used (aom/vpx/etc.)
`encoder_params`                  | string  | Encoding parameters
`ffmpeg_params`                   | string  | (Optional) Encoding parameters
`min_frames`                      | integer | (Optional) Minimum amount of frames per segment
`max_frames`                      | integer | (Optional) Maximum amount of frames per segment
`on_complete`                     | string  | (Optional) Action to perform on completion of encode
`priority`                        | number  | (Optional) Priority
`id`                              | string  | (Optional) Project id

**Example:**

```json
{
  "input": ["1.mkv", "2.mkv"],
  "encoder": "aom",
  "encoder_params": "--lag-in-frames=25 -b 10 --cpu-used=3",
  "min_frames": 25,
  "max_frames": 160,
  "on_complete": "merge",
  "priority": -0.5
}
```
**Returns:**

JSON object

Property                          | Type    | Description
----------------------------------|---------|------------
`success`                         | bool    | Success
`reason`                          | string  | Reason if success is false

**Example:**

```json
{"success": true}
```

```json
{"success": false, "reason": "You are not allowed to do this!"}
```

## Modify Project ##

Name: `/api/modify_project/<projectid>`

Method: POST

**Parameters:**

Parameter                         | Type    | Description
----------------------------------|---------|------------
`projectid`                       | string  | Id representing the project

Requires a Json object in the body

#### incomplete

Property                          | Type    | Description
----------------------------------|---------|------------
`priority`                        | integer | Priority in the encoding queue

**Example:**

`/api/modify_project/1`

**Returns:**

JSON object

Property                          | Type    | Description
----------------------------------|---------|------------
`success`                         | bool    | Success
`reason`                          | string  | Reason if success is false

**Example:**

```json
{"success": true}
```

```json
{"success": false, "reason": "Project does not exist."}
```


## Delete Project ##

Name: `/api/delete_project/<projectid>`

Method: POST

**Parameters:**

Parameter                         | Type    | Description
----------------------------------|---------|------------
`projectid`                       | string  | Id representing the project

**Example:**

`/api/delete_project/1`

**Returns:**

JSON object

Property                          | Type    | Description
----------------------------------|---------|------------
`success`                         | bool    | Success
`reason`                          | string  | Reason if success is false

**Example:**

```json
{"success": true}
```

```json
{"success": false, "reason": "Project does not exist."}
```

## Preview Scene ##

Name: `/scene/<projectid>/<scene>`

Method: GET

**Parameters:**

Parameter                         | Type    | Description
----------------------------------|---------|------------
`projectid`                       | string  | Id representing the project
`scene`                           | integer | Id representing the scene / segment

**Example:**

`/scene/1/1`

**Returns:**

The encoded scene / segment

## Structs ##

### Project ###

Key                               | Type    | Description
----------------------------------|---------|------------
`projectid`                       | string  | Id representing the project
`input`                           | string  | Input filename / path
`frames`                          | integer | Number of currently encoded frames
`total_frames`                    | integer | Total number of frames to be encoded
`jobs`                            | integer | Number of completed jobs / segments
`total_jobs`                      | integer | Total number of jobs / segments
`status`                          | string  | Current status of the project
`encoder`                         | string  | Encoder name (aom/vp9/etc.)
`encoder_params`                  | string  | Encoder parameters
`scenes`                          | array   | List of Scenes - See Scene struct below
`priority`                        | integer | Priority in the encoding queue
`workers`                         | array   | List of workers

### Scene ###

Key                               | Type    | Description
----------------------------------|---------|------------
`filesize`                        | integer | Size of encoded file in bytes (0 for incomplete)
`frames`                          | integer | Number of frames in the segment
`encoder_params`                  | string  | Specific encoder parameters for the segment
