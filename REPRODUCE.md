# Как воспроизвести результаты

Все команды выполняются из корня клона. Демо работает на Python 3.12 с SQLite FTS5. Загрузчик бинарников для опытов рассчитан на macOS arm64. Загрузки, индексы и результаты новых запусков хранятся в `.state/`. Сохранённые измерения находятся в `results/`.

## Открыть wiki

```sh
python3.12 app.py init
python3.12 app.py serve --engine sqlite
```

Откройте главную страницу по адресу, который напечатает сервер. Для примера завершённого изменения доступны две статические редакции и документ-основание. [Инструкция до изменения](demo/p/orbit-guide/v1.html#s1) · [После изменения](demo/p/orbit-guide/v2.html#s1) · [Принятое решение](demo/p/orbit-decision/v1.html#s1)

## Проверить результаты без модели и облака

Первый пример считает попадания по позициям в каждой из 64 сохранённых пар и считает зачтённые ответы по отдельным обезличенным оценкам. Для него достаточно стандартной библиотеки Python.

```sh
python3.12 - <<'PY'
import json
from pathlib import Path

root = Path("results")
rows = json.loads((root / "rovo-2026-11-22/paired-search.json").read_text())["rows"]
paired = root / "agent-policy-v3-2026-11-23/paired"
grades = json.loads((paired / "agent-grades.json").read_text())
labels = json.loads((paired / "blind-key.json").read_text())
for arm in ("xerj", "rovo"):
    hits = sum(any(item["id"] in row["gold"] for item in row["arms"][arm]["positions"][:5]) for row in rows)
    answers = [grades[label] for label, ref in labels.items() if ref["arm"] == arm]
    print(arm, f"поиск {hits}/{len(rows)}, ответы {sum(g['pass'] for g in answers)}/{len(answers)}")
PY
```

Ожидаемые значения для XERJ — 56/64 и 1/16, для Rovo — 52/64 и 0/16. Время в JSON выражено в миллисекундах.

## Подготовить среду и данные

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest -v
.venv/bin/python fetch_assets.py --agent
```

Последняя команда скачает два среза Confluence и вопросы из EnterpriseRAG-Bench v1.0.0, XERJ rc.104, multilingual-E5-small INT8, llama.cpp b12541 и Qwen3-4B Q4_K_M. У всех загружаемых файлов проверяется SHA-256. Для опыта без LLM можно опустить `--agent`. Исходные лицензии и версии доступны у поставщиков. [Корпус](https://github.com/onyx-dot-app/EnterpriseRAG-Bench/releases/tag/v1.0.0) · [XERJ](https://github.com/xerj-org/xerj/releases/tag/v1.0.0-rc.104) · [E5](https://huggingface.co/Xenova/multilingual-e5-small) · [llama.cpp](https://github.com/ggml-org/llama.cpp/releases/tag/b12541) · [Qwen3-4B](https://huggingface.co/Qwen/Qwen3-4B-GGUF)

Для полного набора английских источников скачайте архив и установите манифест 512 страниц. Эти шаги нужны для облачного сравнения, тестов с полным корпусом и контроля с готовыми выдержками.

```sh
.venv/bin/python - <<'PY'
from fetch_assets import DATA, fetch
from scale_xerj import ARCHIVE, SHA256
fetch(DATA + ARCHIVE.name, ARCHIVE, SHA256)
PY
mkdir -p .state/retry-cloud-nested
cp benchmarks/pages.csv .state/retry-cloud-nested/pages.csv
```

Для интеграционных тестов запустите XERJ в отдельном каталоге. Выберите три соседних свободных порта начиная с `WIKI_XERJ_PORT`. Сервис модели при включении занимает также 19391. Обёртка завершит дочерние процессы после выполнения команды. Для PDF-импорта нужен `pdftotext` из Poppler.

```sh
export WIKI_XERJ_PORT=24361
WIKI_QA_PDF=benchmarks/control.pdf WIKI_QA_XERJ=1 .venv/bin/python services.py --data-dir "$PWD/.state/qa/index" \
  --log-dir .state/qa -- .venv/bin/python -m unittest -v
```

## Сравнить локальные движки

```sh
.venv/bin/python app.py init --public
.venv/bin/python embedding.py
.venv/bin/python services.py -- .venv/bin/python benchmark.py
.venv/bin/python services.py -- .venv/bin/python diagnose_dense.py
.venv/bin/python fulltext_eval.py
.venv/bin/python services.py --llama -- .venv/bin/python payload_eval.py
```

Для индексации полного релиза запустите `.venv/bin/python services.py -- .venv/bin/python scale_xerj.py`. Начальными входами для роста до 10 ГБ служат этот индекс и сохранённые метаданные под `.state/full-release-lexical-v1/`. Программа роста принимает отдельный каталог данных, checkpoint и целевое число байт. На 40 ГБ использовалось продолжение с checkpoint 10 ГБ. [Код нагрузочного теста](scale_growth.py) · [Число документов, время и объём индекса](results/retry-2026-11-22/xerj-40gb.json)

## Парный прогон с собственным Confluence Cloud

Для сопоставимых данных нужен собственный тестовый сайт, отдельное закрытое пространство и доступ к семантическому инструменту Rovo `search`. Идентификатор пространства — числовой ID, ключ — строка. Укажите свои значения в переменных ниже. Хост задаётся без схемы.

```sh
export WIKI_ATLASSIAN_SITE='your-site.atlassian.net'
export WIKI_ATLASSIAN_SPACE='YOURSPACE'
export WIKI_ATLASSIAN_SPACE_ID='123456'
export WIKI_XERJ_PORT=24361
run_dir=.state/rovo-repeat
mkdir -p "$run_dir"
```

1. Подготовьте архив для импорта. Первая команда упаковывает полный релиз и требует места под промежуточные файлы. Вторая выбирает воспроизводимые 512 страниц. Проверьте равенство манифестов до импорта.

   ```sh
   .venv/bin/python pack_confluence.py
   .venv/bin/python confluence_batch.py --pages 512 --folder 'Wiki Benchmark Import 512 Nested 20261122' --output .state/import-512
   cmp benchmarks/pages.csv .state/import-512/pages.csv
   ```

2. Импортируйте `.state/import-512/corpus.zip` через HTML-импорт Confluence в своё закрытое пространство. Импорт может создать новое пространство. В этом случае укажите выданный ключ и ID в переменных. В исходном опыте импорт завершился частично, и недостающие страницы были созданы из тех же HTML через MCP. Для такого повтора предусмотрен отдельный скрипт записи для закрытого тестового пространства с названием, начинающимся с `Wiki Benchmark Private Replay `. Параметры и проверка имени приведены в скрипте. [Восстановление набора страниц из архива](replay_private_html.py)

3. Из того же каталога в отдельном терминале запустите OAuth-мост и подтвердите чтение в браузере. Оставьте процесс работающим на время теста.

   ```sh
   .venv/bin/python atlassian_oauth.py --rovo --socket .state/rovo-repeat/atl.sock
   ```

4. В первом терминале зафиксируйте эталонные ответы и проверьте инвентаризацию. Перед парным прогоном ожидаются 512 сопоставленных страниц и совпадение текстов 132 эталонов. Программа поиска проверяет эти условия и строит индекс в новом каталоге.

   ```sh
   manifest=benchmarks/pages.csv
   .venv/bin/python rovo_read_pilot.py --freeze --output "$run_dir"
   .venv/bin/python retry_cloud_search.py --socket "$run_dir/atl.sock" \
     --site "$WIKI_ATLASSIAN_SITE" --space "$WIKI_ATLASSIAN_SPACE" \
     --manifest "$manifest" --output "$run_dir/cloud-inventory.json"
   .venv/bin/python services.py --data-dir "$PWD/$run_dir/index" --log-dir "$run_dir" -- \
     .venv/bin/python rovo_benchmark.py --socket "$run_dir/atl.sock" --manifest "$manifest" \
     --inventory "$run_dir/cloud-inventory.json" --output "$run_dir/paired-search.json"
   ```

5. Запустите 16 пар с тем же индексом. Новый клиент допускает пять поисков, пять чтений и 180 секунд на попытку. При прерывании `--resume` продолжает с сохранённых попыток при совпадении настроек.

   ```sh
   .venv/bin/python services.py --llama --data-dir "$PWD/$run_dir/index" --log-dir "$run_dir" -- \
     .venv/bin/python rovo_read_pilot.py --socket "$run_dir/atl.sock" --manifest "$manifest" \
     --inventory "$run_dir/cloud-inventory.json" --output "$run_dir"
   ```

6. Оцените `blind.json` по фактам из `grading.json`. Для каждой метки укажите `pass` и `reason` по формату опубликованного листа. Затем перепроверьте ссылки и рассчитайте метрики.

   ```sh
   .venv/bin/python rovo_read_pilot.py --output "$run_dir" --grades "$run_dir/manual-grades.json" \
     --socket "$run_dir/atl.sock" --inventory "$run_dir/cloud-inventory.json"
   ```

В опубликованных трассах ссылки на закрытый сайт и временные страницы замера заменены ссылочными идентификаторами открытых документов. Перепроверка URL выполняется в собственном прогоне до замены адресов. Исторические хеши клиента относятся к измеренной версии. Публичная копия получает настройки сайта извне. [Лист оценок](results/agent-policy-v3-2026-11-23/paired/agent-grades.json) · [Методика и изменения клиента](POLICY-AND-EVIDENCE.md)

## Контроль с готовыми выдержками

После загрузки полного архива и манифеста можно воспроизвести пакет источников и дать его локальной модели. Облачное подключение для этого опыта не требуется.

```sh
.venv/bin/python services.py --llama -- .venv/bin/python evidence_control.py \
  --model Qwen3-4B-Q4_K_M.gguf --output .state/oracle-repeat-4b
```

Для опыта с 8B скачайте закреплённую версию весов и используйте режим с подпунктами, как в сохранённом замере.

```sh
.venv/bin/python - <<'PY'
from corpus import STATE
from fetch_assets import fetch
name = "Qwen3-8B-Q4_K_M.gguf"
revision = "7c41481f57cb95916b40956ab2f0b139b296d974"
fetch(f"https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/{revision}/{name}",
      STATE / "models" / name, "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785")
PY
.venv/bin/python services.py --llama --llama-model .state/models/Qwen3-8B-Q4_K_M.gguf -- \
  .venv/bin/python evidence_control.py --model Qwen3-8B-Q4_K_M.gguf \
    --modes by_part --output .state/oracle-repeat-8b
```

Контроль записывает ответы по вопросам, SHA-256 пакетов и параметры модели. Один запуск на вопрос даёт отдельное наблюдение. Для сравнения качества примените одинаковый критерий полноты и проверки источников к каждому новому ответу. [Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B-GGUF) · [Оценки 4B](results/agent-policy-v3-2026-11-23/oracle-4b/grades.json) · [Оценки 8B](results/agent-policy-v3-2026-11-23/oracle-8b/grades.json)

## Эксперимент с двумя способами цитирования

План, рубрика и пороги сохранены до замера. [План опыта](CITATION-EXPERIMENT.md) · [Замороженные вопросы](results/citation-ab-2026-11-23/tasks.json)

Подготовьте окружение, архив, тот же манифест и индекс `rovo-paired-512` по инструкции парного прогона выше. Для отдельного каталога опыта задайте `run_dir=.state/citation-ab` до подготовки OAuth-моста и индекса. Запустите инвентаризацию с выводом в `"$run_dir/inventory.json"`. В каталоге должны находиться подтверждённая инвентаризация, сокет `atl.sock` и каталог данных XERJ `index/`. В окружении нужны `WIKI_ATLASSIAN_SITE`, `WIKI_ATLASSIAN_SPACE`, `WIKI_ATLASSIAN_SPACE_ID` и свободный `WIKI_XERJ_PORT`. OAuth-мост должен оставаться доступным на время опыта и проверки ссылок.

```sh
run_dir=.state/citation-ab
.venv/bin/python citation_sample.py --output "$run_dir" --inventory "$run_dir/inventory.json"
cmp "$run_dir/tasks.json" results/citation-ab-2026-11-23/tasks.json
.venv/bin/python services.py --llama --data-dir "$PWD/$run_dir/index" --log-dir "$run_dir" -- \
  .venv/bin/python citation_experiment.py --folder "$run_dir" run
```

Выполняются 128 последовательных попыток. После прерывания повторите последнюю команду с `run --resume`. Оценку проводят по `blind.json` и эталонам `grading.json`, до просмотра соответствий в `blind-key.json`. Для каждого утверждения учитывайте только текст фактически указанной цитаты. Повторное открытие привязок и экспорт трасс выполняются отдельно.

```sh
.venv/bin/python citation_experiment.py --folder "$run_dir" verify
.venv/bin/python citation_experiment.py --folder "$run_dir" verify --online
.venv/bin/python citation_publish.py "$run_dir" "$run_dir/attempts-public.json"
```

Запишите смысловые решения в `grades.json` по обезличенным меткам. Каждое решение содержит булевы поля `meaning_pass` и `wrong_meaning_with_evidence` и текстовую причину `reason`. Расчёт требует все четыре условия для каждого вопроса и объединяет слепую оценку с перепроверенными ссылками.

```sh
.venv/bin/python citation_metrics.py "$run_dir" > "$run_dir/summary.json"
```
