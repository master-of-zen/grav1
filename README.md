# grav1
## distributed network av1 / vp9 encoder

[web client](https://encode.grass.moe)

### preview

![client](https://github.com/wwww-wwww/grav1/raw/master/images/client.gif)

<img src="https://github.com/wwww-wwww/grav1/raw/master/images/website.png" width="600">

### requirements
server:
```
flask
flask-cors
wsgiserver
```
client:  
```
requests
```
client (system):
```
curses
ffmpeg
aomenc (vmaf optional)
```

### usage

this only supports aomenc and libvpx-vp9 provided by ffmpeg  
(I don't plan on supporting other encoders)

start webserver (default port: 7899)  
`python server.py`  
`python server.py --port 1234`

starting up a worker  
`python grav1c.py http://target --workers=4`  

access the server through the [web client](https://encode.grass.moe) (incomplete)

## web ui api
If you want your program to support my web client, here are the specifications:

## Get Projects ##

Name: `/api/get_projects`

Method: GET

**Parameters:**

None

**Returns:**

HTTP Status Code                  | Response
----------------------------------|------------
200                               | JSON List of objects

**Example:**

```json
[
  {
    "projectid": "1",
    "input": "some filename",
    "frames": 50,
    "total_frames": 100,
    "fps": 5,
    "jobs": 1,
    "total_jobs": 2,
    "status": "ready",
    "encoder_params": "-b 10 --cpu-used=3 --end-usage=q --cq-level=20",
    "encoder": "aom",
    "scenes": {
      "00001": {"filesize": 427911, "frames": 50, "encoder_params": ""},
      "00002": {"filesize": 503284, "frames": 50, "encoder_params": ""}
    },
    "priority": 0,
    "workers": []
  },
  {
    "projectid": "4",
    ...
  }
  ...
]
```

## Create Project ##

Name: `/api/add_project`

Method: POST

**Parameters:**

Requires a Json object in the body

Property                          | Type    | Description
----------------------------------|---------|------------
`input`                           | list    | List of filenames / paths
`encoder`                         | string  | Encoder used (aom/vp9/etc.)
`encoder_params`                  | string  | Encoding parameters
`threshold`                       | integer | (Optional) Threshold for scene detection
`min_frames`                      | integer | (Optional) Minimum amount of frames per segment
`max_frames`                      | integer | (Optional) Maximum amount of frames per segment

**Example:**

```json
{
  "input": ["1.mkv", "2.mkv"],
  "encoder": "aom",
  "encoder_params": "-b 10 --cpu-used=3",
  "threshold": 50,
  "min_frames": 140,
  "max_frames": 160
}
```
**Returns:**

HTTP Status Code                  | Response
----------------------------------|------------
200                               | JSON object below

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

HTTP Status Code                  | Response
----------------------------------|------------
200                               | JSON object below

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

HTTP Status Code                  | Response
----------------------------------|------------
200                               | JSON object below

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
`fps`                             | float   | Current fps
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
