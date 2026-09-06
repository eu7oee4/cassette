# companion 选型自测

设计定稿见 `../PLAN_companion.md` §10.2。回答一个问题：**哪个底座值得带上场**。
这是离线选型 eval，不是产品指标（那是 §11 的事），两者别混。

## 跑

```sh
pip install anthropic openai pyyaml
cp .env.example .env   # 填 key 后 source .env
python run_eval.py scenarios/qiuzhao.yaml --models deepseek,haiku,sonnet,opus --runs 3
```

## 评

```sh
python score.py auto results/qiuzhao/deepseek                        # 第一层：程序直接数
python score.py judge results/qiuzhao/deepseek results/qiuzhao/haiku # 第二层：配对盲评
# 第三层：自己读 results/ 里的 transcript，重点读①②打分冲突的场次
```

## 注意

- 用户模拟器自动选和被测**不同族**的模型（测 deepseek 时 haiku 演用户，反之 deepseek 演）。
- Claude 侧走 console 按量 key（直调 Messages API，生产同构）。**别走订阅壳**——壳是混杂变量；**别走注水中转**——模型身份就是实验变量。
- 整个矩阵是短对话，全跑一遍几块钱量级。
- 场景库从小起步：3 persona × 2 剧本先跑起来，让它告诉你缺哪种探针。
- results/ 不入库。
