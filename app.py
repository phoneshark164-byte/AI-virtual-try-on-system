import os
import time
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO
import base64
import re

from PIL import Image, ImageDraw
from flask import Flask, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    jwt_required,
    get_jwt_identity,
    get_jwt,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError, IntegrityError
from openai import OpenAI

# 尝试导入 rembg 用于自动抠图
try:
    from rembg import remove

    REMBG_AVAILABLE = True
except ImportError:
    REMBG_AVAILABLE = False

app = Flask(__name__)

# ================= 配置区 =================
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["JWT_SECRET_KEY"] = os.environ.get("JWT_SECRET_KEY", "jwt-dev-secret-change-in-production")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=int(os.environ.get("JWT_EXPIRE_HOURS", "24")))

# 数据库：未设置 DATABASE_URL 时默认使用本地 SQLite，
# 请设置环境变量：DATABASE_URL=mysql+pymysql://用户:密码@主机:3306/库名
_base_dir = os.path.dirname(os.path.abspath(__file__))
_sqlite_path = os.path.join(_base_dir, "virtual_tryon.db").replace("\\", "/")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", f"sqlite:///{_sqlite_path}")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# 使用项目绝对路径，避免在 PyCharm 不同工作目录下写到错误位置
UPLOAD_FOLDER = os.path.join(_base_dir, "static", "uploads")
RESULT_FOLDER = os.path.join(_base_dir, "static", "results")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(RESULT_FOLDER, exist_ok=True)

CORS(
    app,
    resources={r"/*": {"origins": "*"}},
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization"],
)

db = SQLAlchemy(app)
jwt = JWTManager(app)
MODEL_BLACKLIST = set()


@jwt.expired_token_loader
def _expired_token(*_):
    return jsonify({"error": "登录已过期，请重新登录"}), 401


@jwt.invalid_token_loader
def _invalid_token(_err):
    return jsonify({"error": "无效的 Token"}), 401


@jwt.unauthorized_loader
def _missing_token(_err):
    return jsonify({"error": "请先登录"}), 401


# ================= 数据库模型 =================
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=True)
    role = db.Column(db.String(20), nullable=False, default="user")  # user | admin
    height = db.Column(db.Float, default=165.0)
    chest = db.Column(db.Float, default=85.0)
    waist = db.Column(db.Float, default=65.0)
    created_at = db.Column(db.DateTime, default=db.func.now())


class TryOnRecord(db.Model):
    __tablename__ = "try_on_records"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    person_image_path = db.Column(db.String(255), nullable=False)
    clothing_image_path = db.Column(db.String(255), nullable=False)
    result_image_path = db.Column(db.String(255), nullable=False)
    style_preference = db.Column(db.String(100))
    size_recommendation = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=db.func.now())
    in_wardrobe = db.Column(db.Boolean, nullable=False, default=False)


def _ensure_user_columns():
    """为已有表补充 password_hash、role 字段（MySQL）。"""
    try:
        insp = inspect(db.engine)
        if "users" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("users")}
        if "password_hash" not in cols:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255) NULL"))
        if "role" not in cols:
            with db.engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'user'")
                )
    except Exception as e:
        print("迁移提示（可忽略若表已最新）:", e)


def _ensure_tryon_wardrobe_column():
    """为历史库补充 in_wardrobe 字段。"""
    try:
        insp = inspect(db.engine)
        if "try_on_records" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("try_on_records")}
        if "in_wardrobe" in cols:
            return
        dialect = db.engine.dialect.name
        with db.engine.begin() as conn:
            if dialect == "sqlite":
                conn.execute(text("ALTER TABLE try_on_records ADD COLUMN in_wardrobe INTEGER NOT NULL DEFAULT 0"))
            else:
                conn.execute(text("ALTER TABLE try_on_records ADD COLUMN in_wardrobe TINYINT(1) NOT NULL DEFAULT 0"))
    except Exception as e:
        print("try_on_records 迁移提示:", e)


def admin_required(fn):
    """仅管理员可访问"""

    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        claims = get_jwt()
        if claims.get("role") != "admin":
            return jsonify({"error": "需要管理员权限"}), 403
        return fn(*args, **kwargs)

    return wrapper


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in {"png", "jpg", "jpeg"}


def get_api_key():
    try:
        with open("api_key.txt", "r") as file:
            return file.read().strip()
    except FileNotFoundError:
        return None


def get_openai_client():
    """获取 OpenAI 兼容客户端"""
    api_key = get_api_key()
    if not api_key:
        return None
    return OpenAI(
        api_key=api_key,
        base_url="https://118api.cn/v1"
    )


def get_candidate_models():
    """返回候选模型列表（OpenAI 兼容格式）"""
    return [
        "gemini-2.5-flash",
    ]


def request_tryon_image(client, model_name, prompt_text, person_bytes, clothing_bytes):
    """
    使用 OpenAI 兼容接口调用图像生成
    """
    person_base64 = base64.b64encode(person_bytes).decode('utf-8')
    clothing_base64 = base64.b64encode(clothing_bytes).decode('utf-8')

    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{person_base64}"}},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{clothing_base64}"}},
                ]
            }
        ],
        temperature=0.5,
        max_tokens=2048,
    )
    return response


# ================= JWT 认证 =================


@app.route("/api/auth/register", methods=["POST", "OPTIONS"])
def register():
    """注册普通用户（不可自助注册管理员）"""
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "用户名与密码不能为空"}), 400
    if len(password) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400
    try:
        if User.query.filter_by(username=username).first():
            return jsonify({"error": "用户名已存在"}), 409
        user = User(
            username=username,
            password_hash=generate_password_hash(password),
            role="user",
        )
        db.session.add(user)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "用户名已存在"}), 409
    except OperationalError as e:
        db.session.rollback()
        print("注册数据库错误:", e)
        return jsonify({"error": "数据库不可用，请检查 DATABASE_URL 或本机数据库服务"}), 503
    except Exception as e:
        db.session.rollback()
        print("注册异常:", e)
        return jsonify({"error": f"注册失败: {str(e)}"}), 500

    token = create_access_token(
        identity=str(user.id),
        additional_claims={"role": user.role, "username": user.username},
    )
    return jsonify(
        {
            "access_token": token,
            "user": {"id": user.id, "username": user.username, "role": user.role},
        }
    )


@app.route("/api/auth/login", methods=["POST", "OPTIONS"])
def login():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    try:
        user = User.query.filter_by(username=username).first()
    except OperationalError as e:
        print("登录数据库错误:", e)
        return jsonify({"error": "数据库不可用，请检查 DATABASE_URL 或本机数据库服务"}), 503
    if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "用户名或密码错误"}), 401
    token = create_access_token(
        identity=str(user.id),
        additional_claims={"role": user.role, "username": user.username},
    )
    return jsonify(
        {
            "access_token": token,
            "user": {"id": user.id, "username": user.username, "role": user.role},
        }
    )


@app.route("/api/auth/me", methods=["GET"])
@jwt_required()
def me():
    uid = int(get_jwt_identity())
    user = User.query.get(uid)
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    return jsonify(
        {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "height": user.height,
            "chest": user.chest,
            "waist": user.waist,
        }
    )


@app.route("/api/admin/users", methods=["GET"])
@admin_required
def admin_list_users():
    users = User.query.order_by(User.id).all()
    results = []
    for u in users:
        record_count = TryOnRecord.query.filter_by(user_id=u.id).count()
        wardrobe_count = TryOnRecord.query.filter_by(user_id=u.id, in_wardrobe=True).count()
        latest_record = (
            TryOnRecord.query.filter_by(user_id=u.id)
            .order_by(TryOnRecord.created_at.desc(), TryOnRecord.id.desc())
            .first()
        )
        results.append(
            {
                "id": u.id,
                "username": u.username,
                "role": u.role,
                "created_at": u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else None,
                "record_count": record_count,
                "wardrobe_count": wardrobe_count,
                "last_tryon_at": latest_record.created_at.strftime("%Y-%m-%d %H:%M:%S")
                if latest_record and latest_record.created_at
                else None,
            }
        )
    return jsonify({"users": results})


@app.route("/api/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_change_user_role(user_id):
    data = request.get_json(silent=True) or {}
    new_role = (data.get("role") or "").strip().lower()
    if new_role not in {"user", "admin"}:
        return jsonify({"error": "role 只能是 user 或 admin"}), 400

    target = User.query.get(user_id)
    if not target:
        return jsonify({"error": "用户不存在"}), 404

    # 避免把最后一个管理员降级为 user
    if target.role == "admin" and new_role != "admin":
        admin_count = User.query.filter_by(role="admin").count()
        if admin_count <= 1:
            return jsonify({"error": "至少保留一个管理员"}), 400

    target.role = new_role
    db.session.commit()
    return jsonify(
        {
            "success": True,
            "user": {"id": target.id, "username": target.username, "role": target.role},
        }
    )


@app.route("/api/admin/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def admin_reset_user_password(user_id):
    data = request.get_json(silent=True) or {}
    new_password = (data.get("new_password") or "").strip()
    if len(new_password) < 6:
        return jsonify({"error": "新密码至少 6 位"}), 400

    target = User.query.get(user_id)
    if not target:
        return jsonify({"error": "用户不存在"}), 404

    target.password_hash = generate_password_hash(new_password)
    db.session.commit()
    return jsonify({"success": True, "message": "密码已重置"})


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def admin_delete_user(user_id):
    operator_id = int(get_jwt_identity())
    if operator_id == user_id:
        return jsonify({"error": "不能删除当前登录的管理员账号"}), 400

    target = User.query.get(user_id)
    if not target:
        return jsonify({"error": "用户不存在"}), 404

    if target.role == "admin":
        admin_count = User.query.filter_by(role="admin").count()
        if admin_count <= 1:
            return jsonify({"error": "至少保留一个管理员"}), 400

    TryOnRecord.query.filter_by(user_id=target.id).delete()
    db.session.delete(target)
    db.session.commit()
    return jsonify({"success": True, "message": "用户已删除"})


# ================= 业务接口（需登录） =================


@app.route("/api/upload", methods=["POST"])
@jwt_required()
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "没有找到文件"}), 400

    file = request.files["file"]
    upload_type = request.form.get("type", "person")
    auto_remove_bg = request.form.get("auto_rmbg", "false").lower() == "true"

    if file.filename == "":
        return jsonify({"error": "文件名为空"}), 400

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        timestamp = int(time.time())
        parts = filename.rsplit(".", 1)
        if len(parts) < 2:
            return jsonify({"error": "文件名缺少扩展名"}), 400
        file_ext = parts[1].lower()
        new_filename = f"{upload_type}_{timestamp}.{file_ext}"
        filepath = os.path.join(UPLOAD_FOLDER, new_filename)

        file.save(filepath)

        if upload_type == "clothing" and auto_remove_bg and REMBG_AVAILABLE:
            try:
                input_img = Image.open(filepath)
                output_img = remove(input_img)
                rmbg_filename = f"clothing_nobg_{timestamp}.png"
                rmbg_filepath = os.path.join(UPLOAD_FOLDER, rmbg_filename)
                output_img.save(rmbg_filepath)
                filepath = rmbg_filepath
                new_filename = rmbg_filename
            except Exception as e:
                print(f"抠图失败: {e}")

        file_url = f"/static/uploads/{new_filename}"
        return jsonify({"success": True, "file_url": file_url, "file_path": filepath})

    return jsonify({"error": "不支持的文件类型"}), 400


@app.route("/api/try-on", methods=["POST"])
@jwt_required()
def generate_try_on():
    uid = int(get_jwt_identity())
    data = request.json
    person_image_path = data.get("person_image_path")
    clothing_image_path = data.get("clothing_image_path")
    user_id = uid

    height = int(data.get("height", 165))
    chest = int(data.get("chest", 85))
    waist = int(data.get("waist", 65))
    style_pref = data.get("style_preference", "日常休闲 (Casual)")

    if not person_image_path or not clothing_image_path:
        return jsonify({"error": "缺少必要图片参数"}), 400

    api_key = get_api_key()
    if not api_key:
        return jsonify({"error": "服务器未配置 Gemini API Key"}), 500

    if chest < 80:
        size = "S"
    elif chest < 90:
        size = "M"
    elif chest < 100:
        size = "L"
    elif chest < 110:
        size = "XL"
    else:
        size = "XXL"

    if waist < 65:
        fit_status = "腰部微松，舒适"
    elif waist < 80:
        fit_status = "版型合身"
    else:
        fit_status = "建议选大一码以防紧绷"
    size_recommendation = f"{size} 码 ({fit_status})"

    base_prompt = "Generate a high-quality virtual try-on image showing the person wearing the clothing. Preserve facial features and pose."
    body_instruction = f"Parametric Body Model applied: Height {height}cm, Chest {chest}cm, Waist {waist}cm. Ensure the generated clothing strictly maps to these physical dimensions."
    integrated_prompt = f"{base_prompt}\n\n[System Directives]\n- Style: {style_pref}\n- Proportions: {body_instruction}"

    try:
        client = get_openai_client()
        if not client:
            return jsonify({"error": "API Key 配置失败"}), 500

        person_img = Image.open(person_image_path)
        clothing_img = Image.open(clothing_image_path)

        def image_to_bytes(img):
            buf = BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        person_bytes = image_to_bytes(person_img)
        clothing_bytes = image_to_bytes(clothing_img)

        response = None
        last_error = None
        for model_name in get_candidate_models():
            if model_name in MODEL_BLACKLIST:
                continue
            try:
                response = request_tryon_image(
                    client=client,
                    model_name=model_name,
                    prompt_text=integrated_prompt,
                    person_bytes=person_bytes,
                    clothing_bytes=clothing_bytes,
                )
                break
            except Exception as e:
                last_error = e
                print(f"模型调用失败 {model_name}: {e}")
                err_text = str(e).lower()
                if "not found" in err_text or "not supported for generatecontent" in err_text:
                    MODEL_BLACKLIST.add(model_name)

        if response is None and last_error:
            raise last_error

        result_filename = None
        result_filepath = None

        # 从 OpenAI 兼容响应中提取图片
        if response and response.choices:
            message = response.choices[0].message
            if message.content:
                content_text = str(message.content)
                # 从 Markdown 格式提取图片 URL: ![image](url)
                url_match = re.search(r'!\[.*?\]\((http[s]?://[^\)]+)\)', content_text)
                if url_match:
                    img_url = url_match.group(1)
                    result_filename = f"result_{int(time.time())}.png"
                    result_filepath = os.path.join(RESULT_FOLDER, result_filename)
                    import requests
                    img_resp = requests.get(img_url, timeout=30)
                    with open(result_filepath, "wb") as f:
                        f.write(img_resp.content)

        if not result_filepath:
            return jsonify({"error": "AI 生成失败，未返回图片数据"}), 500

        new_record = TryOnRecord(
            user_id=user_id,
            person_image_path=person_image_path,
            clothing_image_path=clothing_image_path,
            result_image_path=result_filepath,
            style_preference=style_pref,
            size_recommendation=size_recommendation,
        )
        db.session.add(new_record)
        db.session.commit()

        result_url = f"/static/results/{result_filename}"

        return jsonify(
            {
                "success": True,
                "record_id": new_record.id,
                "result_url": result_url,
                "size_recommendation": size_recommendation,
            }
        )

    except Exception as e:
        print("API Error:", e)
        return jsonify({"error": f"生成过程发生异常: {str(e)}"}), 500


@app.route("/api/wardrobe", methods=["GET"])
@jwt_required()
def list_wardrobe():
    uid = int(get_jwt_identity())
    rows = (
        TryOnRecord.query.filter_by(user_id=uid, in_wardrobe=True)
        .order_by(TryOnRecord.created_at.desc())
        .all()
    )
    items = []
    for r in rows:
        items.append(
            {
                "id": r.id,
                "result_url": f"/static/results/{os.path.basename(r.result_image_path)}",
                "person_url": f"/static/uploads/{os.path.basename(r.person_image_path)}",
                "clothing_url": f"/static/uploads/{os.path.basename(r.clothing_image_path)}",
                "style": r.style_preference,
                "size_recommendation": r.size_recommendation,
                "date": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else None,
            }
        )
    return jsonify({"success": True, "items": items})


@app.route("/api/wardrobe/add", methods=["POST"])
@jwt_required()
def add_to_wardrobe():
    uid = int(get_jwt_identity())
    data = request.get_json(silent=True) or {}
    record_id = data.get("record_id")
    if not record_id:
        return jsonify({"error": "缺少 record_id"}), 400
    rec = TryOnRecord.query.filter_by(id=int(record_id), user_id=uid).first()
    if not rec:
        return jsonify({"error": "记录不存在"}), 404
    rec.in_wardrobe = True
    db.session.commit()
    return jsonify({"success": True, "message": "已加入我的衣柜"})


@app.route("/api/wardrobe/<int:record_id>", methods=["DELETE"])
@jwt_required()
def remove_from_wardrobe(record_id):
    uid = int(get_jwt_identity())
    rec = TryOnRecord.query.filter_by(id=record_id, user_id=uid).first()
    if not rec:
        return jsonify({"error": "记录不存在"}), 404
    rec.in_wardrobe = False
    db.session.commit()
    return jsonify({"success": True, "message": "已从衣柜移除"})


@app.route("/api/records/<int:user_id>", methods=["GET"])
@jwt_required()
def get_user_records(user_id):
    current_id = int(get_jwt_identity())
    role = get_jwt().get("role")
    if role != "admin" and current_id != user_id:
        return jsonify({"error": "无权查看他人记录"}), 403

    records = TryOnRecord.query.filter_by(user_id=user_id).order_by(TryOnRecord.created_at.desc()).all()
    results = []
    for r in records:
        results.append(
            {
                "id": r.id,
                "result_url": f"/static/results/{os.path.basename(r.result_image_path)}",
                "person_url": f"/static/uploads/{os.path.basename(r.person_image_path)}",
                "clothing_url": f"/static/uploads/{os.path.basename(r.clothing_image_path)}",
                "style": r.style_preference,
                "size_recommendation": r.size_recommendation,
                "date": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else None,
                "in_wardrobe": bool(getattr(r, "in_wardrobe", False)),
            }
        )
    return jsonify({"success": True, "records": results})


@app.route("/api/models", methods=["GET"])
def list_models():
    """调试接口：列出当前 API Key 可见模型。"""
    api_key = get_api_key()
    if not api_key:
        return jsonify({"error": "服务器未配置 Gemini API Key"}), 500
    try:
        client = genai.Client(api_key=api_key)
        items = []
        for m in client.models.list():
            name = getattr(m, "name", "")
            methods = getattr(m, "supported_generation_methods", None)
            items.append({"name": name, "methods": methods})
        return jsonify({"success": True, "models": items})
    except Exception as e:
        return jsonify({"error": f"获取模型列表失败: {str(e)}"}), 500


@app.route("/")
def index_page():
    """浏览器访问 http://127.0.0.1:5000/ 打开前端，避免 file:// 跨域导致登录失败"""
    return send_from_directory(_base_dir, "index.html")


def _seed_demo_records():
    """为 demo 用户补充演示数据：3 条历史记录，其中 2 条加入衣柜。"""
    demo = User.query.filter_by(username="demo").first()
    if not demo:
        return

    # 生成占位图，确保前端有图可看
    sample_styles = [
        ("日常休闲 (Casual)", "M 码 (版型合身)"),
        ("商务通勤 (Business)", "L 码 (版型合身)"),
        ("街头潮流 (Streetwear)", "M 码 (腰部微松，舒适)"),
    ]

    base_specs = [
        ("person_demo_1.png", (93, 173, 226), "人物A"),
        ("person_demo_2.png", (56, 189, 248), "人物B"),
        ("clothing_demo_1.png", (167, 139, 250), "服装A"),
        ("clothing_demo_2.png", (251, 146, 60), "服装B"),
    ]

    def _ensure_img(path, color, label, kind="result", force=False):
        if os.path.exists(path) and not force:
            return

        w, h = 640, 860
        img = Image.new("RGB", (w, h), (245, 247, 250))
        draw = ImageDraw.Draw(img)

        # 渐变背景（避免纯色）
        for y in range(h):
            ratio = y / max(1, h - 1)
            r = int(248 - 18 * ratio)
            g = int(250 - 20 * ratio)
            b = int(255 - 28 * ratio)
            draw.line([(0, y), (w, y)], fill=(r, g, b))

        # 装饰卡片
        card_margin = 36
        draw.rounded_rectangle(
            [(card_margin, card_margin), (w - card_margin, h - card_margin)],
            radius=36,
            fill=(255, 255, 255),
            outline=(224, 231, 255),
            width=3,
        )

        accent = color
        if kind == "person":
            # 人像简笔轮廓
            draw.ellipse([(250, 180), (390, 320)], fill=(255, 224, 189), outline=(214, 184, 148), width=2)
            draw.rounded_rectangle([(210, 320), (430, 640)], radius=90, fill=(accent[0], accent[1], accent[2]))
            draw.text((250, 700), "PERSON", fill=(71, 85, 105))
        elif kind == "clothing":
            # 服装简笔图
            draw.polygon([(190, 240), (450, 240), (510, 370), (430, 420), (400, 640), (240, 640), (210, 420), (130, 370)],
                         fill=(accent[0], accent[1], accent[2]), outline=(99, 102, 241))
            draw.rounded_rectangle([(260, 240), (380, 300)], radius=20, fill=(248, 250, 252))
            draw.text((240, 700), "CLOTHING", fill=(71, 85, 105))
        else:
            # 结果图：人像 + 服装覆盖层
            draw.ellipse([(248, 150), (392, 292)], fill=(255, 224, 189), outline=(214, 184, 148), width=2)
            draw.rounded_rectangle([(210, 292), (430, 650)], radius=90, fill=(107, 114, 128))
            draw.rounded_rectangle([(220, 330), (420, 620)], radius=70, fill=(accent[0], accent[1], accent[2]))
            draw.text((245, 710), "TRY-ON RESULT", fill=(30, 41, 59))

        draw.text((card_margin + 14, card_margin + 10), label, fill=(100, 116, 139))
        img.save(path, format="PNG")

    img_paths = {}
    for name, color, label in base_specs:
        folder = RESULT_FOLDER if name.startswith("result_") else UPLOAD_FOLDER
        path = os.path.join(folder, name)
        kind = "person" if "person" in name else "clothing"
        _ensure_img(path, color, label, kind=kind, force=True)
        img_paths[name] = path

    # 为结果图单独准备 3 张
    result_colors = [
        (99, 102, 241), (16, 185, 129), (14, 165, 233)
    ]
    result_names = []
    for i, color in enumerate(result_colors, start=1):
        name = f"result_demo_{i}.png"
        path = os.path.join(RESULT_FOLDER, name)
        _ensure_img(path, color, f"Result {i}", kind="result", force=True)
        result_names.append(name)

    # 修复已有 demo 记录中可能丢失的图片路径，并约束条数
    target_count = 3
    wardrobe_count = 2
    time_points = [
        datetime.now() - timedelta(days=4, hours=1),
        datetime.now() - timedelta(days=2, hours=5),
        datetime.now() - timedelta(hours=7),
    ]

    existing_rows = (
        TryOnRecord.query.filter_by(user_id=demo.id)
        .order_by(TryOnRecord.id.asc())
        .all()
    )

    # 超出目标数量时删除多余旧数据
    if len(existing_rows) > target_count:
        for row in existing_rows[target_count:]:
            db.session.delete(row)
        existing_rows = existing_rows[:target_count]

    for idx, row in enumerate(existing_rows):
        person_name = "person_demo_1.png" if idx % 2 == 0 else "person_demo_2.png"
        clothing_name = "clothing_demo_1.png" if idx % 2 == 0 else "clothing_demo_2.png"
        result_name = result_names[idx % len(result_names)]
        row.person_image_path = img_paths[person_name]
        row.clothing_image_path = img_paths[clothing_name]
        row.result_image_path = os.path.join(RESULT_FOLDER, result_name)
        row.style_preference = sample_styles[idx % len(sample_styles)][0]
        row.size_recommendation = sample_styles[idx % len(sample_styles)][1]
        row.in_wardrobe = idx < wardrobe_count
        row.created_at = time_points[idx]

    existing = len(existing_rows)
    if existing >= target_count:
        db.session.commit()
        return

    for i in range(existing, target_count):
        style, size_text = sample_styles[i]
        rec = TryOnRecord(
            user_id=demo.id,
            person_image_path=img_paths["person_demo_1.png" if i % 2 == 0 else "person_demo_2.png"],
            clothing_image_path=img_paths["clothing_demo_1.png" if i % 2 == 0 else "clothing_demo_2.png"],
            result_image_path=os.path.join(RESULT_FOLDER, result_names[i]),
            style_preference=style,
            size_recommendation=size_text,
            in_wardrobe=(i < wardrobe_count),
            created_at=time_points[i],
        )
        db.session.add(rec)

    db.session.commit()
    print("[初始化] 已同步 3 条历史试穿记录（含 2 条衣柜记录）")


def init_database():
    """启动时建表并写入默认账号（导入 app 时即执行，避免未建表导致登录/注册失败）"""
    with app.app_context():
        uri = app.config["SQLALCHEMY_DATABASE_URI"]
        print("[数据库]", "SQLite" if uri.startswith("sqlite") else "MySQL/其他", uri[:80] + ("..." if len(uri) > 80 else ""))
        db.create_all()
        _ensure_user_columns()
        _ensure_tryon_wardrobe_column()
        db.session.commit()

        if not User.query.filter_by(username="admin").first():
            admin_pw = os.environ.get("ADMIN_INITIAL_PASSWORD", "admin123")
            admin = User(
                username="admin",
                password_hash=generate_password_hash(admin_pw),
                role="admin",
            )
            db.session.add(admin)
            db.session.commit()
            print("[初始化] 已创建管理员 admin，密码见 ADMIN_INITIAL_PASSWORD（默认 admin123）")

        if not User.query.filter_by(username="demo").first():
            demo = User(
                username="demo",
                password_hash=generate_password_hash("demo123456"),
                role="user",
            )
            db.session.add(demo)
            db.session.commit()
            print("[初始化] 已创建演示用户 demo / demo123456")

        _seed_demo_records()


init_database()


if __name__ == "__main__":
    print("[前端] 请在浏览器打开: http://127.0.0.1:5000/  （勿用本地文件双击打开 index.html）")
    app.run(host="0.0.0.0", port=5000, debug=True)
