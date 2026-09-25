# 经营看板 + 问答服务
# 换数据或知识库：make rebuild DATA_DIR=/path/data KB_DIR=/path/kb

PY ?= .venv/bin/python
PORT ?= 8000
export DATA_DIR KB_DIR VAR_DIR LLM_BASE_URL LLM_API_KEY LLM_MODEL

.PHONY: setup rebuild run test clean

setup:          ## 建虚拟环境并装依赖
	uv venv --python 3.12
	uv pip install --python $(PY) -r requirements.txt

rebuild:        ## 从 DATA_DIR 与 KB_DIR 重建清洗表和检索索引
	$(PY) -m kbqa.rebuild

run:            ## 启动服务，默认 8000 端口
	$(PY) -m uvicorn kbqa.server:app --host 127.0.0.1 --port $(PORT)

test:           ## 跑测试
	$(PY) -m pytest tests -q

clean:          ## 删掉清洗表
	rm -rf var
