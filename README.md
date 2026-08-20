# DeepRadio
From: jensenlei, cindysha, sihanwang

## 快速开始

```bash
# 1. 创建并激活 gnuradio 环境
conda env create -f environment.yml 
conda activate gnuradio

# 2. 配置 Agent API (从模板复制并填写你的 key)
cp .env.example .env
# 编辑 .env, 填入 GRC_AGENT_BASE_URL / GRC_AGENT_API_KEY / GRC_AGENT_MODEL
# 未配置时 Agent 会降级为确定性建图

# 3. 启动 GRC (GUI)
cd DeepRadio          # 确保在项目根目录
PYTHONPATH=$PWD python -m grc.main --gtk
```


