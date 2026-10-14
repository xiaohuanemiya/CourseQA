import argparse
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Base, engine
from .models import Application, Knowledge, KnowledgeBase, ModelProfile, Question, Relation, User
from .security import password_hash
from .services.knowledge import publish
from .services.learning import publish_application


def seed(db, password):
    admin = db.scalar(select(User).where(User.username == "admin"))
    if not admin:
        admin = User(username="admin", password_hash=password_hash(password), role="admin")
        db.add(admin)
        db.flush()
    if len(db.scalars(select(Application).where(Application.active)).all()) >= 2:
        return
    generation = ModelProfile(name="演示 · 资料证据展示", provider="demo", purpose="generation", model="evidence-only")
    embedding = ModelProfile(name="演示 · 词项向量", provider="demo", purpose="embedding", model="hash-bigrams", dimension=256)
    db.add_all([generation, embedding])
    db.flush()
    for title, description, chapter, entries in [
        ("极限与导数", "从极限出发，理解连续与可导。", "第一章 · 极限与导数", [
            ("函数极限", "函数极限描述自变量趋近某点时函数值趋近的值。计算时必须注明趋近点与方向。", r"\lim_{x\to a} f(x)=L", "concept"),
            ("函数连续", "函数在一点连续，需要该点函数值存在、该点极限存在，并且极限等于函数值。", r"\lim_{x\to a} f(x)=f(a)", "concept"),
            ("可导与连续", "函数在某点可导，则在该点连续；连续不一定可导。例如绝对值函数在零点连续但不可导。", "", "theorem"),
            ("幂函数求导", "在公式有定义并满足可导条件的区间，幂函数的导数为指数乘以降一次幂。正实数区间可使用一般实指数。", r"(x^n)'=nx^{n-1}", "formula"),
        ]),
        ("积分专项", "掌握原函数、积分常数与分部积分。", "第二章 · 积分", [
            ("原函数与不定积分", "若 F 的导数为 f，则 F 是 f 的原函数。不定积分答案在指定区间写为 F(x)+C，C 为任意常数。", r"\int f(x)\,dx=F(x)+C", "concept"),
            ("幂函数积分", "在正实数区间，当 n 不等于 -1 时，x 的 n 次方的不定积分为 x 的 n+1 次方除以 n+1，再加积分常数。", r"\int x^n\,dx=\frac{x^{n+1}}{n+1}+C,\quad n\ne-1", "formula"),
            ("分部积分", "由乘积求导法则可得分部积分公式，使用时应选择适当的 u 和 dv，并保留积分常数。", r"\int u\,dv=uv-\int v\,du", "formula"),
        ]),
    ]:
        existing = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == title+" · 示例库"))
        if existing:
            db.refresh(existing)
            for prior in db.scalars(select(Application)):
                if existing.id in prior.draft["kb_ids"] and not prior.active:
                    publish_application(db, prior.id, admin.id)
            continue
        kb = KnowledgeBase(name=title+" · 示例库", description="人工编写的演示资料，不是用户教材。", extraction_profile_id=generation.id)
        db.add(kb)
        db.flush()
        nodes = []
        for name, content, latex, kind in entries:
            node = Knowledge(kb_id=kb.id, kind=kind, title=name, content=content,
                attributes={"chapter": chapter, "latex": latex, "aliases": [], "conditions": "请结合原文条件使用"},
                sources=[{"document_id": None, "locator": "演示资料 · "+name, "excerpt": content,
                          "note": "平台人工编写的示例，用于验证流程；非用户提供教材"}])
            db.add(node)
            db.flush()
            nodes.append(node)
        for a, b in zip(nodes, nodes[1:]):
            db.add(Relation(kb_id=kb.id, source_id=a.id, target_id=b.id, kind="prerequisite"))
        for n in range(2, 7):
            integration = title == "积分专项"
            db.add(Question(kb_id=kb.id, title=f"计算 {'∫ x^'+str(n)+' dx' if integration else 'd(x^'+str(n)+')/dx'}。" +
                "设 x > 0。", kind="antiderivative" if integration else "expression",
                answer=f"x^{n+1}/{n+1}+C" if integration else f"{n}*x^{n-1}",
                explanation="代入幂函数积分公式，保留 C。" if integration else "使用幂函数求导公式，指数下降一次。",
                knowledge_ids=[nodes[1].id if integration else nodes[-1].id],
                attributes={"chapter": chapter, "difficulty": "easy", "assumption": "positive"}))
        db.commit()
        publish(db, kb.id, admin.id)
        application = Application(draft={"name": title+"研习", "description": description, "prompt": "你是一位严谨的高数助教。",
            "model_profile_id": generation.id, "embedding_profile_id": embedding.id,
            "kb_ids": [kb.id], "features": ["chat", "quiz"]})
        db.add(application)
        db.commit()
        publish_application(db, application.id, admin.id)


def main():
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "admin", "demo"])
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-env", default="ADMIN_PASSWORD")
    args = parser.parse_args()
    if args.command == "init":
        if engine().dialect.name != "sqlite":
            raise SystemExit("PostgreSQL 请使用 alembic upgrade head")
        Base.metadata.create_all(engine())
        print("本地开发数据库已初始化")
        return
    password = os.environ.get(args.password_env, "")
    if len(password) < 12:
        raise SystemExit(f"请设置 {args.password_env}，至少 12 个字符")
    with Session(engine(), expire_on_commit=False) as db:
        if args.command == "demo":
            from .config import settings
            if not settings().allow_demo_provider:
                raise SystemExit("示例初始化需要 ALLOW_DEMO_PROVIDER=true")
            seed(db, password)
        else:
            if db.scalar(select(User).where(User.username == args.username)):
                raise SystemExit("账号已存在，未覆盖密码")
            db.add(User(username=args.username, password_hash=password_hash(password), role="admin"))
            db.commit()
    print("初始化完成；密码未写入日志")


if __name__ == "__main__":
    main()
