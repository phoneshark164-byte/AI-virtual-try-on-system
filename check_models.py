import os

# 必须在 import genai 之前设置网络代理！
# 请将 7890 替换为你实际的代理软件端口
os.environ['HTTP_PROXY'] = 'http://127.0.0.1:7897'
os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:7897'

import google.generativeai as genai

# 替换为你真实的 API Key
GOOGLE_API_KEY = "AIzaSyCOab6syoXSZZravCj58sWZv2ZZEeTu2HQ"

try:
    genai.configure(api_key=GOOGLE_API_KEY)
    print("正在连接 Google AI Studio 查询可用模型...\n")

    found_flash = False
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            print(f"可用模型: {m.name}")
            if 'flash' in m.name.lower():
                found_flash = True

    if not found_flash:
        print("\n⚠️ 没有找到带有 'flash' 字样的模型。")

except Exception as e:
    print(f"查询失败，错误信息: {e}")