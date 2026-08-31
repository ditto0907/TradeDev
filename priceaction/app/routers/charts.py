import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from storage import db

router = APIRouter()


@router.get("/api/charts")
async def list_charts():
    return db.get_all_charts()


@router.post("/api/charts")
async def save_chart_endpoint(request: Request):
    data = await request.json()
    chart_id = db.save_chart(
        chart_id=data.get("id"),
        name=data["name"],
        symbol=data.get("symbol", ""),
        resolution=data.get("resolution", ""),
        content=data.get("content", ""),
        timestamp=data.get("timestamp", int(time.time())),
    )
    return {"id": chart_id}


@router.get("/api/charts/{chart_id}")
async def get_chart(chart_id: int):
    content = db.get_chart_content(chart_id)
    if content is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"content": content}


@router.delete("/api/charts/{chart_id}")
async def delete_chart(chart_id: int):
    db.remove_chart(chart_id)
    return {"ok": True}


@router.get("/api/study_templates")
async def list_study_templates():
    return db.get_all_study_templates()


@router.post("/api/study_templates")
async def save_study_template_endpoint(request: Request):
    data = await request.json()
    db.save_study_template(data["name"], data.get("content", ""))
    return {"ok": True}


@router.get("/api/study_templates/{name}")
async def get_study_template(name: str):
    content = db.get_study_template_content(name)
    if content is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"content": content}


@router.delete("/api/study_templates/{name}")
async def delete_study_template(name: str):
    db.remove_study_template(name)
    return {"ok": True}


@router.get("/api/drawing_templates/{tool_name}")
async def list_drawing_templates(tool_name: str):
    return db.get_drawing_templates(tool_name)


@router.post("/api/drawing_templates")
async def save_drawing_template_endpoint(request: Request):
    data = await request.json()
    db.save_drawing_template(data["tool_name"], data["template_name"], data.get("content", ""))
    return {"ok": True}


@router.get("/api/drawing_templates/{tool_name}/{template_name}")
async def get_drawing_template(tool_name: str, template_name: str):
    content = db.load_drawing_template(tool_name, template_name)
    if content is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"content": content}


@router.delete("/api/drawing_templates/{tool_name}/{template_name}")
async def delete_drawing_template(tool_name: str, template_name: str):
    db.remove_drawing_template(tool_name, template_name)
    return {"ok": True}


@router.get("/api/chart_templates")
async def list_chart_templates():
    return db.get_all_chart_templates()


@router.post("/api/chart_templates")
async def save_chart_template_endpoint(request: Request):
    data = await request.json()
    db.save_chart_template(data["name"], json.dumps(data.get("content", {})))
    return {"ok": True}


@router.get("/api/chart_templates/{name}")
async def get_chart_template(name: str):
    content = db.get_chart_template_content(name)
    if content is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return json.loads(content)


@router.delete("/api/chart_templates/{name}")
async def delete_chart_template(name: str):
    db.remove_chart_template(name)
    return {"ok": True}
