# -*- coding: utf-8 -*-
"""
BetterRAG FastAPI 服务主入口
集成自适应混合检索、OpenAI兼容接口、三轨归因闭环、7天沙箱挑刺与自治管理看板
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.engine.config import DEFAULT_DB_PATH, DEFAULT_WORKSPACE_DIR
from src.engine.storage import Storage
from src.engine.retrieve import HybridRetriever
from src.engine.credit import CreditEngine
from src.engine.reconcile import DailyReconciler
from src.engine.ingest import IngestionEngine
from src.engine.arbitrate import ConflictManager
from src.engine.probation import ProbationManager
from src.engine.conflict_pipe import ConflictPipeline
from src.api.routes_chat import router as chat_router
from src.api.routes_admin import router as admin_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务生命周期管理：初始化存储、检索器、冲突管理器、举证管理器与调度器"""
    db_path = os.getenv("DB_PATH", str(DEFAULT_DB_PATH))
    storage = Storage(db_path)
    retriever = HybridRetriever(storage)
    credit_engine = CreditEngine(storage)
    reconciler = DailyReconciler(storage)
    conflict_manager = ConflictManager(storage)
    probation_manager = ProbationManager(storage)
    conflict_pipe = ConflictPipeline(storage, conflict_manager)

    # 启动时执行一次目录初始同步（如果 workspace 存在）
    workspace_dir = os.getenv("DATA_DIR", str(DEFAULT_WORKSPACE_DIR))
    if Path(workspace_dir).exists():
        try:
            ingest_engine = IngestionEngine(storage)
            ingest_engine.sync_workspace(workspace_dir)
        except Exception:
            pass

    app.state.storage = storage
    app.state.retriever = retriever
    app.state.credit_engine = credit_engine
    app.state.reconciler = reconciler
    app.state.conflict_manager = conflict_manager
    app.state.probation_manager = probation_manager
    app.state.conflict_pipe = conflict_pipe

    # 启动进程内轻量调度器：每天凌晨 03:00 执行衰减协调与举证期到期审查
    scheduler = AsyncIOScheduler()
    scheduler.add_job(reconciler.reconcile, "cron", hour=3, minute=0)
    scheduler.add_job(probation_manager.check_and_promote, "cron", hour=3, minute=30)
    scheduler.start()
    app.state.scheduler = scheduler

    yield

    # 优雅停机
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass
    storage.close()


def create_app() -> FastAPI:
    """构建 FastAPI 应用"""
    app = FastAPI(
        title="BetterRAG Production Engine",
        version="1.0.0",
        description="单机零运维、自供证、自愈型企业知识库引擎",
        lifespan=lifespan,
    )

    # 允许跨域（支持第三方客户端如 NextChat, Dify, Web UI 接入）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 挂载业务路由
    app.include_router(chat_router)
    app.include_router(admin_router)

    # 挂载自治管理前端看板
    @app.get("/dashboard", response_class=HTMLResponse, tags=["Dashboard"])
    async def dashboard_view():
        html_path = Path(__file__).parent / "web" / "index.html"
        if html_path.exists():
            return html_path.read_text(encoding="utf-8")
        return "<h3>BetterRAG Dashboard file not found</h3>"

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("src.main:app", host="0.0.0.0", port=port, reload=True)
