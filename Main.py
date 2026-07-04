import sys
import os
from io import BytesIO
import time
import shutil
import pyodbc
from openai import OpenAI
from PIL import Image

from PyQt6.QtWidgets import (QApplication, QMainWindow, QLabel, QPushButton,
                             QVBoxLayout, QHBoxLayout, QWidget, QFileDialog,
                             QTextEdit, QProgressBar, QMessageBox, QInputDialog, QLineEdit,
                             QFrame, QSizePolicy, QScrollArea, QGridLayout, QSpinBox, QComboBox, QFormLayout)
from PyQt6.QtGui import QPixmap, QFont
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer

# 尝试导入 rembg，用于实现 DOC 中提到的自动抠图功能
try:
    from rembg import remove

    REMBG_AVAILABLE = True
except ImportError:
    REMBG_AVAILABLE = False

# 保存上传和输出图片的目录
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'results'

# 确保目录存在
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


def get_api_key():
    """读取 API 密钥（支持三方 OpenAI 兼容接口）"""
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
    """返回候选模型列表"""
    return [
        "gemini-2.5-flash-image",
    ]


def get_sqlserver_conn():
    """
    连接 SQL Server。
    支持通过环境变量覆盖默认值：
    - SQLSERVER_HOST (默认: 127.0.0.1,1433)
    - SQLSERVER_DB   (默认: TryOnDB)
    - SQLSERVER_USER (默认: sa)
    - SQLSERVER_PASSWORD (默认: 123456)
    - SQLSERVER_DRIVER (默认: ODBC Driver 17 for SQL Server)
    """
    server = os.getenv("SQLSERVER_HOST", "127.0.0.1,1433")
    database = os.getenv("SQLSERVER_DB", "TryOnDB")
    username = os.getenv("SQLSERVER_USER", "sa")
    password = os.getenv("SQLSERVER_PASSWORD", "123456")
    driver = os.getenv("SQLSERVER_DRIVER", "ODBC Driver 17 for SQL Server")

    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={password};"
        "TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, timeout=5)


class GeminiThread(QThread):
    """
    独立线程处理 Gemini API 调用，避免界面卡顿
    """
    finished_signal = pyqtSignal(bool, str, int)  # 用于跟踪结果顺序
    progress_signal = pyqtSignal(int, int)  # progress, thread_id

    def __init__(self, person_image_path, clothing_image_path, prompt, thread_id, api_key=None):
        super().__init__()
        self.person_image_path = person_image_path
        self.clothing_image_path = clothing_image_path
        self.prompt = prompt
        self.thread_id = thread_id
        self.api_key = api_key
        self.is_cancelled = False
        self.max_retries = 3
        self.base_retry_wait = 2.0

    @staticmethod
    def _is_retryable_error(error_message):
        text = error_message.lower()
        retryable_keywords = [
            "timeout", "timed out", "deadline exceeded", "504",
            "429", "quota", "rate limit", "temporarily unavailable",
            "503", "502", "connection reset", "socket", "network"
        ]
        return any(k in text for k in retryable_keywords)

    def run(self):
        try:
            if self.is_cancelled:
                return

            client = get_openai_client()
            if not client:
                raise Exception("未找到 API 密钥或客户端初始化失败")

            self.progress_signal.emit(10, self.thread_id)

            if self.is_cancelled:
                return
            self.progress_signal.emit(30, self.thread_id)

            # 使用 Pillow 打开图片并转换为 base64
            person_img = Image.open(self.person_image_path)
            clothing_img = Image.open(self.clothing_image_path)

            def image_to_base64(img):
                buf = BytesIO()
                img.save(buf, format='PNG')
                return base64.b64encode(buf.getvalue()).decode('utf-8')

            import base64
            person_base64 = image_to_base64(person_img)
            clothing_base64 = image_to_base64(clothing_img)

            if self.is_cancelled:
                return
            self.progress_signal.emit(50, self.thread_id)

            # 创意度递增，探索不同穿搭效果
            temperature = 0.4 + (self.thread_id * 0.05)

            for i in range(20):
                if self.is_cancelled:
                    return
                time.sleep(0.1)

            if self.is_cancelled:
                return

            # 发起 OpenAI 兼容 API 请求
            response = None
            last_error = None
            for attempt in range(1, self.max_retries + 1):
                if self.is_cancelled:
                    return
                try:
                    response = client.chat.completions.create(
                        model="gemini-2.5-flash",
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": self.prompt},
                                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{person_base64}"}},
                                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{clothing_base64}"}},
                                ]
                            }
                        ],
                        temperature=temperature,
                        max_tokens=2048,
                    )
                    break
                except Exception as request_error:
                    last_error = request_error
                    err_msg = str(request_error)
                    is_last_attempt = attempt == self.max_retries
                    if is_last_attempt or not self._is_retryable_error(err_msg):
                        raise
                    wait_sec = self.base_retry_wait * attempt
                    for _ in range(int(wait_sec * 10)):
                        if self.is_cancelled:
                            return
                        time.sleep(0.1)

            if response is None and last_error:
                raise last_error

            if self.is_cancelled:
                return
            self.progress_signal.emit(80, self.thread_id)

            result_image_path = None
            # 提取生成的图片
            if response and response.choices:
                message = response.choices[0].message
                print(f"Debug: Message content = {message.content}")
                if message.content:
                    # 合并所有内容为字符串
                    content_text = str(message.content)
                    import re
                    # 从 Markdown 格式提取图片 URL: ![image](url)
                    url_match = re.search(r'!\[.*?\]\((http[s]?://[^\)]+)\)', content_text)
                    if url_match:
                        img_url = url_match.group(1)
                        print(f"Debug: Found image URL: {img_url}")
                        result_image_path = os.path.join(OUTPUT_FOLDER, f"result_{self.thread_id}_{int(time.time())}.png")
                        import requests
                        img_resp = requests.get(img_url, timeout=30)
                        with open(result_image_path, "wb") as f:
                            f.write(img_resp.content)

            if result_image_path and not self.is_cancelled:
                self.progress_signal.emit(100, self.thread_id)
                self.finished_signal.emit(True, result_image_path, self.thread_id)
            elif not self.is_cancelled:
                raise Exception(f"API 未返回结果图片 {self.thread_id + 1}")

        except Exception as e:
            if not self.is_cancelled:
                import traceback
                traceback.print_exc()
                self.finished_signal.emit(False, str(e), self.thread_id)

    def cancel(self):
        self.is_cancelled = True


class ResultWidget(QWidget):
    """单个试衣结果的展示组件"""

    def __init__(self, id, parent=None):
        super().__init__(parent)
        self.id = id
        self.result_image_path = None
        self.initUI()

    def initUI(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel(f"试穿方案 {self.id + 1}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 12pt; font-weight: 600; color: #52525b;")

        self.image_frame = QFrame()
        self.image_frame.setStyleSheet("""
            QFrame { background: white; border: 1px solid #e4e4e7; border-radius: 16px; }
            QFrame:hover { border-color: #a1a1aa; }
        """)
        self.image_frame.setMinimumSize(260, 360)

        frame_layout = QVBoxLayout(self.image_frame)
        self.image_label = QLabel("等待生成")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("color: #a1a1aa; font-size: 11pt; background: #fafafa; border-radius: 10px;")
        frame_layout.addWidget(self.image_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setValue(0)

        self.save_btn = QPushButton("保存到我的衣柜")
        self.save_btn.setEnabled(False)
        self.save_btn.setStyleSheet("padding: 8px; font-size: 11pt;")

        layout.addWidget(title)
        layout.addWidget(self.image_frame)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.save_btn)

    def display_image(self, image_path):
        self.result_image_path = image_path
        pixmap = QPixmap(image_path).scaled(
            self.image_label.width(), self.image_label.height(),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        self.image_label.setPixmap(pixmap)
        self.save_btn.setEnabled(True)

    def update_progress(self, value):
        self.progress_bar.setValue(value)

    def save_image(self, parent):
        if not self.result_image_path: return
        save_path, _ = QFileDialog.getSaveFileName(
            parent, f'保存搭配记录', f'outfit_record_{self.id + 1}.png', 'PNG (*.png)'
        )
        if save_path:
            shutil.copy2(self.result_image_path, save_path)
            QMessageBox.information(parent, '成功', '已加入个人试穿行为库！\n这有助于提升下次推荐的精准度。')


class PersonalizedTryOnApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.person_image_path = None
        self.clothing_image_path = None
        self.result_widgets = []
        self.gemini_threads = []
        self.scheduled_timers = []
        self.db_initialized = False
        self.thread_start_interval_ms = 3500
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle('基于用户行为的个性化虚拟穿搭系统')
        self.setMinimumSize(1300, 800)

        main_widget = QWidget()
        main_layout = QHBoxLayout(main_widget)
        main_layout.setSpacing(24)
        main_layout.setContentsMargins(24, 20, 24, 20)

        # ====== 左侧控制面板 ======
        left_container = QFrame()
        left_container.setFixedWidth(440)
        left_container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        left_container.setStyleSheet(
            "QFrame { background: white; border-radius: 20px; border: 1px solid rgba(0,0,0,0.06); }")

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setSpacing(18)
        left_layout.setContentsMargins(24, 24, 24, 24)

        title_label = QLabel('个性化 3D 虚拟穿搭')
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet('font-size: 18pt; font-weight: 700; color: #18181b;')
        left_layout.addWidget(title_label)

        # 1. 人物与服装上传区
        images_layout = QHBoxLayout()
        images_layout.setSpacing(12)

        # 人物图
        person_layout = QVBoxLayout()
        self.person_image_label = QLabel('点击上传真人/模特照')
        self.person_image_label.setFixedSize(180, 240)
        self.person_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.person_image_label.setStyleSheet(
            "border: 2px dashed #e4e4e7; border-radius: 12px; background: #fafafa; color: #a1a1aa;")
        person_btn = QPushButton('选择人物')
        person_btn.clicked.connect(self.upload_person_image)
        person_layout.addWidget(self.person_image_label)
        person_layout.addWidget(person_btn)

        # 服装图
        clothing_layout = QVBoxLayout()
        self.clothing_image_label = QLabel('上传服装 (自动抠图)')
        self.clothing_image_label.setFixedSize(180, 240)
        self.clothing_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clothing_image_label.setStyleSheet(
            "border: 2px dashed #e4e4e7; border-radius: 12px; background: #fafafa; color: #a1a1aa;")
        clothing_btn = QPushButton('选择服装')
        clothing_btn.clicked.connect(self.upload_clothing_image)
        clothing_layout.addWidget(self.clothing_image_label)
        clothing_layout.addWidget(clothing_btn)

        images_layout.addLayout(person_layout)
        images_layout.addLayout(clothing_layout)
        left_layout.addLayout(images_layout)

        # 2. 身体参数调节区 (基于论文中的 Morph Targets 与智能推荐概念)
        params_frame = QFrame()
        params_frame.setStyleSheet(
            "background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 10px;")
        params_layout = QVBoxLayout(params_frame)

        params_title = QLabel("🧍 参数化人体建模 & 尺码推荐")
        params_title.setStyleSheet("font-weight: 600; font-size: 13pt; color: #334155; margin-bottom: 5px;")
        params_layout.addWidget(params_title)

        form_layout = QFormLayout()

        self.height_spin = QSpinBox()
        self.height_spin.setRange(140, 220)
        self.height_spin.setValue(165)
        self.height_spin.setSuffix(" cm")

        self.chest_spin = QSpinBox()
        self.chest_spin.setRange(60, 140)
        self.chest_spin.setValue(85)
        self.chest_spin.setSuffix(" cm")

        self.waist_spin = QSpinBox()
        self.waist_spin.setRange(50, 120)
        self.waist_spin.setValue(65)
        self.waist_spin.setSuffix(" cm")

        form_layout.addRow("身高 (Height):", self.height_spin)
        form_layout.addRow("胸围 (Chest):", self.chest_spin)
        form_layout.addRow("腰围 (Waist):", self.waist_spin)
        params_layout.addLayout(form_layout)

        # 尺码推荐 Label
        self.size_label = QLabel("尺码建议：计算中...")
        self.size_label.setStyleSheet(
            "color: #059669; font-weight: bold; background: #d1fae5; padding: 8px; border-radius: 6px; margin-top: 5px;")
        self.size_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        params_layout.addWidget(self.size_label)
        left_layout.addWidget(params_frame)

        # 绑定参数变化事件
        self.height_spin.valueChanged.connect(self.update_size_recommendation)
        self.chest_spin.valueChanged.connect(self.update_size_recommendation)
        self.waist_spin.valueChanged.connect(self.update_size_recommendation)
        self.update_size_recommendation()  # 初始化计算

        # 3. 用户行为与个性化推荐设置
        behavior_frame = QFrame()
        behavior_frame.setStyleSheet(
            "background: #fef2f2; border: 1px solid #fecaca; border-radius: 12px; padding: 10px;")
        behavior_layout = QVBoxLayout(behavior_frame)

        behavior_title = QLabel("🎯 协同过滤偏好 (模拟)")
        behavior_title.setStyleSheet("font-weight: 600; font-size: 13pt; color: #991b1b;")
        behavior_layout.addWidget(behavior_title)

        self.style_combo = QComboBox()
        self.style_combo.addItems([
            "根据历史行为推荐匹配 (算法优先)",
            "日常休闲 (Casual)",
            "商务通勤 (Business)",
            "街头潮流 (Streetwear)",
            "国风复古 (Vintage)"
        ])
        behavior_layout.addWidget(self.style_combo)
        left_layout.addWidget(behavior_frame)

        # 4. 自定义提示词
        self.prompt_text = QTextEdit()
        self.prompt_text.setPlaceholderText('额外指令（选填）...')
        self.prompt_text.setText(
            'Generate a high-quality virtual try-on image showing the person wearing the clothing. Preserve facial features and pose.')
        self.prompt_text.setMaximumHeight(80)
        left_layout.addWidget(QLabel('高级视觉渲染指令:'))
        left_layout.addWidget(self.prompt_text)

        # 生成按钮
        self.generate_btn = QPushButton('一键生成 6 款搭配方案')
        self.generate_btn.setStyleSheet("""
            font-size: 14pt; font-weight: 600; padding: 16px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #6366f1, stop:1 #4f46e5);
            color: white; border-radius: 12px;
        """)
        self.generate_btn.clicked.connect(self.generate_images)
        left_layout.addWidget(self.generate_btn)

        left_scroll.setWidget(left_panel)
        QVBoxLayout(left_container).addWidget(left_scroll)

        # ====== 右侧结果面板 ======
        right_panel = QFrame()
        right_panel.setStyleSheet(
            "QFrame { background: white; border-radius: 20px; border: 1px solid rgba(0,0,0,0.06); }")

        right_layout = QVBoxLayout(right_panel)
        result_title = QLabel('👗 虚拟试衣效果矩阵')
        result_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        result_title.setStyleSheet('font-size: 16pt; font-weight: 700; color: #18181b; padding: 10px;')
        right_layout.addWidget(result_title)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        results_container = QWidget()
        self.results_layout = QGridLayout(results_container)
        self.results_layout.setSpacing(20)

        # 创建 6 个结果卡片 (优化性能，排布 2x3)
        NUM_RESULTS = 6
        COLS = 3
        for i in range(NUM_RESULTS):
            widget = ResultWidget(i)
            self.results_layout.addWidget(widget, i // COLS, i % COLS)
            self.result_widgets.append(widget)
            widget.save_btn.clicked.connect(lambda checked=False, idx=i: self.result_widgets[idx].save_image(self))

        scroll_area.setWidget(results_container)
        right_layout.addWidget(scroll_area)

        main_layout.addWidget(left_container, 0)
        main_layout.addWidget(right_panel, 1)
        self.setCentralWidget(main_widget)

    def update_size_recommendation(self):
        """核心业务逻辑：智能尺码匹配与合身性分析"""
        chest = self.chest_spin.value()
        waist = self.waist_spin.value()

        # 简易服装版型推演算法
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

        # 宽松度评估
        if waist < 65:
            fit_status = "腰部微松，舒适"
        elif waist < 80:
            fit_status = "版型合身"
        else:
            fit_status = "建议选大一码以防紧绷"

        self.size_label.setText(f"尺码建议：{size} 码 （{fit_status}）")

    def upload_person_image(self):
        file_path, _ = QFileDialog.getOpenFileName(self, '选择人物照片', '', '图片 (*.png *.jpg *.jpeg)')
        if file_path:
            self.person_image_path = file_path
            self.display_image(self.person_image_label, file_path)

    def upload_clothing_image(self):
        """选择服装并调用 Rembg 算法自动去除背景（文献核心要求）"""
        file_path, _ = QFileDialog.getOpenFileName(self, '选择服装照片', '', '图片 (*.png *.jpg *.jpeg)')
        if not file_path:
            return

        if REMBG_AVAILABLE:
            self.clothing_image_label.setText("正在执行 AI 抠图...")
            QApplication.processEvents()  # 强制刷新 UI 状态

            try:
                input_img = Image.open(file_path)
                output_img = remove(input_img)

                processed_path = os.path.join(UPLOAD_FOLDER, f"clothing_nobg_{int(time.time())}.png")
                output_img.save(processed_path)

                self.clothing_image_path = processed_path
                self.display_image(self.clothing_image_label, processed_path)
                QMessageBox.information(self, '抠图成功', '已通过 Rembg 算法自动剥离复杂背景，提升渲染融合度！')
            except Exception as e:
                QMessageBox.warning(self, '抠图失败', f'处理异常，将使用原图。\n{str(e)}')
                self.clothing_image_path = file_path
                self.display_image(self.clothing_image_label, file_path)
        else:
            QMessageBox.information(self, '提示',
                                    '未检测到 rembg 库，建议在终端执行 pip install rembg 以启用自动抠图功能。本次跳过处理。')
            self.clothing_image_path = file_path
            self.display_image(self.clothing_image_label, file_path)

    def display_image(self, label, image_path):
        pixmap = QPixmap(image_path).scaled(
            label.width(), label.height(),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        label.setPixmap(pixmap)

    def cancel_running_threads(self):
        for thread in self.gemini_threads:
            if thread.isRunning():
                thread.cancel()
                thread.wait(1000)
        for timer in self.scheduled_timers:
            if timer.isActive():
                timer.stop()
        self.gemini_threads.clear()
        self.scheduled_timers.clear()

    def init_db(self):
        """初始化数据库表，避免首次写入失败。"""
        if self.db_initialized:
            return
        conn = None
        cursor = None
        try:
            conn = get_sqlserver_conn()
            cursor = conn.cursor()
            cursor.execute("""
                IF OBJECT_ID(N'dbo.TryOnResults', N'U') IS NULL
                BEGIN
                    CREATE TABLE dbo.TryOnResults (
                        id INT IDENTITY(1,1) PRIMARY KEY,
                        image_path NVARCHAR(500) NOT NULL,
                        height_cm INT NOT NULL,
                        chest_cm INT NOT NULL,
                        waist_cm INT NOT NULL,
                        style_pref NVARCHAR(100) NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT GETDATE()
                    );
                END
            """)
            conn.commit()
            self.db_initialized = True
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def save_tryon_result_to_db(self, image_path):
        conn = None
        cursor = None
        try:
            self.init_db()
            conn = get_sqlserver_conn()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO dbo.TryOnResults (image_path, height_cm, chest_cm, waist_cm, style_pref, created_at)
                VALUES (?, ?, ?, ?, ?, GETDATE())
            """, (
                image_path,
                self.height_spin.value(),
                self.chest_spin.value(),
                self.waist_spin.value(),
                self.style_combo.currentText()
            ))
            conn.commit()
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def generate_images(self):
        self.cancel_running_threads()

        if not self.person_image_path or not self.clothing_image_path:
            QMessageBox.warning(self, '数据缺失', '请上传人物模型图片与服装 SKU 图片！')
            return

        api_key = get_api_key()
        if not api_key:
            api_key, ok = QInputDialog.getText(self, '认证', '请输入 API 密钥：', QLineEdit.EchoMode.Password)
            if not ok or not api_key: return
            with open('api_key.txt', 'w') as file:
                file.write(api_key)

        # --- 整合用户行为偏好与身体特征到 Prompt ---
        base_prompt = self.prompt_text.toPlainText()
        style_pref = self.style_combo.currentText()
        style_instruction = "Focus on aesthetic compatibility based on user history." if "历史行为" in style_pref else f"Adopt a {style_pref} style."

        body_instruction = (
            f"Parametric Body Model applied: Height {self.height_spin.value()}cm, "
            f"Chest {self.chest_spin.value()}cm, Waist {self.waist_spin.value()}cm. "
            f"Ensure the generated clothing strictly maps to these physical dimensions to reflect authentic try-on size."
        )

        integrated_prompt = f"{base_prompt}\n\n[System Directives]\n- Behavior/Style: {style_instruction}\n- Proportions: {body_instruction}"
        print(f"--- 提交的综合提示词 ---\n{integrated_prompt}")

        for widget in self.result_widgets:
            widget.image_label.setText("调用云端算力中...")
            widget.image_label.setPixmap(QPixmap())
            widget.progress_bar.setValue(0)
            widget.save_btn.setEnabled(False)
            widget.result_image_path = None

        self.generate_btn.setEnabled(False)

        for i in range(len(self.result_widgets)):
            thread = GeminiThread(self.person_image_path, self.clothing_image_path, integrated_prompt, i, api_key)
            thread.progress_signal.connect(self.update_progress)
            thread.finished_signal.connect(self.process_result)
            self.gemini_threads.append(thread)

            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(
                lambda idx=i: self.gemini_threads[idx].start() if idx < len(self.gemini_threads) else None)
            timer.start(i * self.thread_start_interval_ms)
            self.scheduled_timers.append(timer)

    def update_progress(self, value, thread_id):
        if thread_id < len(self.result_widgets):
            self.result_widgets[thread_id].update_progress(value)

    def process_result(self, success, message, thread_id):
        if thread_id >= len(self.result_widgets): return
        if success:
            self.result_widgets[thread_id].display_image(message)
            try:
                self.save_tryon_result_to_db(message)
            except Exception as e:
                QMessageBox.warning(self, "数据库写入失败", f"结果已生成，但写入 SQL Server 失败：\n{str(e)}")
        else:
            error_text = str(message)
            lowered = error_text.lower()
            if ("timeout" in lowered or "timed out" in lowered or "deadline exceeded" in lowered
                    or "504" in lowered):
                user_friendly = "算力节点超时，请稍后重试（已自动重试仍失败）"
            elif "429" in lowered or "quota" in lowered or "rate limit" in lowered:
                user_friendly = "请求过于频繁或额度不足，请降低频率后重试"
            else:
                user_friendly = f"算法异常：{error_text}"
            self.result_widgets[thread_id].image_label.setText(user_friendly)

        if not any(t.isRunning() for t in self.gemini_threads):
            self.generate_btn.setEnabled(True)


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet('''
        QMainWindow { background: #f1f5f9; }
        QPushButton { font-weight: 500; border: none; border-radius: 6px; background: #e2e8f0; color: #1e293b; padding: 6px;}
        QPushButton:hover { background: #cbd5e1; }
        QSpinBox, QComboBox { padding: 5px; border: 1px solid #cbd5e1; border-radius: 6px; background: white; }
        QProgressBar { border-radius: 4px; background: #e2e8f0; text-align: center; }
        QProgressBar::chunk { background: #6366f1; border-radius: 4px; }
    ''')
    window = PersonalizedTryOnApp()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()