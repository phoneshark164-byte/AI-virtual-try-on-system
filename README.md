# AI 虚拟试衣

基于 Google Gemini 的桌面应用，通过 AI 生成虚拟试衣效果图。上传人物照片和服装照片，即可生成多张合成试衣图片。

## 功能特性

- 分别上传人物照片与服装照片
- 一次生成 10 种不同风格的试衣效果
- 自定义 AI 提示词，精细调节生成效果
- 支持保存单张结果图片
- 简洁易用的图形界面
- 多任务并行处理，提升生成效率

## 环境要求

- Python 3.8 或更高版本
- Google Gemini API 密钥
- 可访问 Google API 的网络环境

## 安装

### 方式一：使用 EXE（推荐普通用户）

1. 下载 [AI-ClothingTryOn.exe](https://github.com/yourusername/AI-ClothingTryOn/releases)
2. 若无法直接下载 EXE，可下载 [ZIP 包](https://mega.nz/file/pYpkQbzJ#exFxB7T2QhQFbMUzza1xx_KeAajMreSy3MdBgZOKuQM)
3. 解压后运行 `AI-ClothingTryOn.exe`
4. 按提示输入 Google Gemini API 密钥

### 方式二：源码运行（开发者）

1. 克隆项目：

   ```bash
   git clone https://github.com/yourusername/AI-ClothingTryOn.git
   cd AI-ClothingTryOn
   ```

2. 安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

3. 启动程序：

   ```bash
   python Main.py
   ```

## 获取 API 密钥

1. 打开 [Google AI Studio](https://ai.google.dev/)
2. 使用 Google 账号登录
3. 进入 API Keys 页面，创建新密钥
4. 复制密钥到项目目录下的 `api_key.txt`，或首次运行时在弹窗中输入

## 使用说明

1. 启动程序
2. 点击「选择人物照片」上传人物图片
3. 点击「选择服装照片」上传服装图片
4. （可选）在文本框中修改自定义提示词
5. 点击「生成 10 张试衣图」并等待生成
6. 每个结果下方的「保存」按钮可单独保存图片

## 项目结构

```
AI-ClothingTryOn/
├── Main.py           # 主程序
├── requirements.txt  # 依赖列表
├── api_key.txt       # API 密钥（勿提交到仓库）
├── uploads/          # 上传文件缓存
├── results/          # 生成结果保存目录
└── README.md         # 说明文档
```

## 技术栈

- **PyQt6**：图形界面
- **Google Generative AI (Gemini)**：图像生成
- **Pillow**：图像处理
- **QThread**：多线程异步调用 API

## 注意事项

- 使用 Google Gemini API 可能产生费用，请查阅 [Google 定价政策](https://ai.google.dev/pricing)
- 请确保您有权使用上传的图片内容

## 贡献

欢迎提交 Issue 和 Pull Request：

1. Fork 本仓库
2. 创建分支：`git checkout -b feature/your-feature`
3. 提交变更：`git commit -m 'Add some feature'`
4. 推送到分支：`git push origin feature/your-feature`
5. 提交 Pull Request

## 许可证

MIT License，详见 [LICENSE](LICENSE) 文件。

---

Tô Đình Duy · [hoathinh2d.com](https://hoathinh2d.com)
