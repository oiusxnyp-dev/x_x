from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()


@app.get("/", response_class=HTMLResponse)
async def home():
    return """
<!doctype html>
<html lang="ko">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Closed PNL</title>
</head>
<body>
    <h1>Closed PNL</h1>
    <p>gmk-server 정상 작동 중</p>
</body>
</html>
"""
