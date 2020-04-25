# grav1
## distributed network av1 encoder

[eight megabyte encodes](https://grass.moe/8mb.html)

### deps
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
```
curses
ffmpeg
aomenc (vmaf optional)
```

### usage

this only supports aomenc  
(I don't plan on supporting other encoders)

start webserver  
`python server.py`

starting up a worker  
`python grav1c.py http://target --workers=4`  
