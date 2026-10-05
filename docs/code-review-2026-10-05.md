# Мультиагентное code review — 5 октября 2026

Проверен AgentCrew на коммите `d32a437b9a984b0c5c7c27743d6107725143cf3b`. Три независимых ревьюера прочитали код своих областей, соответствующие требования `TZ-agentcrew.md` и тесты. Основной агент отдельно проверил TUI, стартовые шаблоны и стыки публичных команд с контроллером, повторил воспроизведения P1. Первый круг проходил без изменений исходников.

| Ревьюер | Область |
|---|---|
| `review_workflows` | develop/review/fix, критерии принятия, JUnit-доказательства, находки и отчёты |
| `review_runtime_git` | Git/worktree/apply, SQLite, очередь, supervisor, процессы, tmux и постоянные команды |
| `review_providers_cli` | Codex/Claude adapters, worker, CLI, конфигурация, doctor и smoke |
| Основной агент | TUI, scaffold, независимое подтверждение и взаимодействия между областями |

Исходная полная проверка: `.venv/bin/pytest -q` — **279 passed, 9 skipped in 129.99 s**. Первый круг выявил шесть P1. Перекрёстная проверка исправлений и дополнительных точек аварии выявила ещё два P1. P0 не выявлены. Исходный вердикт: исправления обязательны.

## Подтверждённые P1

Локации ниже относятся к проверенному коммиту; после исправлений номера строк могут измениться.

| ID | Исходная локация | Воспроизведение и влияние |
|---|---|---|
| R1 | `providers/base.py:196` | Живой процесс закрывает stdin/stdout/stderr. Worker переходит в длинный `wait()`, перестаёт проверять cancel и продлевать lease. Локальный процесс написал файл после запроса отмены и вернул код 0. Обратный случай тоже подтверждён: завершившийся родитель оставляет child с открытыми pipe, и остановка группы откладывается до EOF; child успевает записать файл. |
| R2 | `queue.py:199`, `worker.py:48` | После аварии writer оставляет частичный патч. Разрешённый `queue retry --inspect-writer` запускает новую попытку, но проверка чистого baseline отвергает её до вызова провайдера. Сохранённую работу нельзя продолжить предусмотренной командой. |
| R3 | `review/workflow.py:261` | Checks успели сохранить PASS до потери worker. Повтор на том же кандидате даёт FAIL, но оценка выбирает старую первую запись: `outcome=accepted`, отчёт complete. |
| R4 | `review/workflow.py:218`, `queue.py:210` | Упавший reviewer приводит к `completed/partial`, phase=report. Повтор роли проходит успешно, но не восстанавливает evaluate: workflow остаётся running/report/partial до таймаута. |
| R5 | `develop/workflow.py:192`, `review/workflow.py:134`, `git_snapshot.py:243`, `queue.py:91` | Падение после freeze и постановки первого reviewer создаёт при восстановлении второй candidate с тем же SHA. Старое задание захватывается, его результат отвергается fencing, а claimed удерживает seat и блокирует новую проверку. |
| R6 | `queue.py:250`, `develop/workflow.py:133`, `supervisor.py:214` | Публичное generic `queue complete --no-artifact` принимает задание plan/implement без обязательного `data`. Следующая оценка вызывает необработанный `KeyError`, который завершает общий supervisor. |
| R7 | `review/aggregate.py:_normalized`, `review/workflow.py:persist_findings` | Сопоставление причин стирает регистр и пробелы в detail/evidence. После решения «Root намеренно admin» новая находка «root ошибочно admin» получает то же dismissal, и после повторного ревью возникает ложное accepted. Точечный возврат прежней нормализации в памяти воспроизводит accepted; исправленный код даёт rejected и две разные находки. |
| R8 | `develop/workflow.py:implement`, `develop/workflow.py:revise` | Падение после создания writer task, но до сохранения новой phase оставляет созданную ветку при старой phase. Следующая оценка пытается создать эту ветку заново и постоянно падает с branch already exists. Окно затрагивает первую реализацию и ревизию. |

Воспроизведения использовали временные Git-репозитории, SQLite и локальные тестовые процессы. Реальные модели не вызывались. Для исправления R4 требуется также учитывать новые находки повторённой роли: ранее `persist_findings` возвращал старый список целиком, если он уже существовал.

## Подтверждённые P2

Эти замечания не входят в запрошенное исправление P0/P1.

| ID | Локация/функция | Что остаётся |
|---|---|---|
| N1 | `tui.py:action_manual` | Ручное подтверждение сохраняется, но не возобновляет needs_attention/evaluate. CLI `confirm` выполняет нужное возобновление. |
| N2 | `review/workflow.py:persist_findings`, `review/report.py:build_report` | Отчёт corroborated finding теряет полное происхождение и модели обоих независимых авторов. |
| N3 | `cli.py:finding_decision` | Audited dismissal меняет находку, но не пересчитывает уже завершённый rejected outcome; обычный apply продолжает отказывать. |
| N4 | `cli.py:check_providers` | Review проверяет доступность провайдеров planner/implementer, хотя запускает только correctness/tests. Отсутствующий провайдер неиспользуемой роли блокирует ревью. |
| N5 | `worker.py:execute` | Native session ID и valid metadata публикуются до окончательного task fencing. Поздний отвергнутый результат может стать контекстом следующего resume. |
| N6 | `teams.py:up`, `queue.py:claim` | Team сохраняет max_parallel_agents внутри repo_config, а claim читает верхний уровень и использует default 2 вместо настроенного 1. |

## Границы проверки

Не выполнялись оплачиваемые Codex/Claude workflows, настоящий native resume, проверка ограничений установленного Claude, macOS и пользовательский пилот. Локальные protocol fixtures проверяют код контроллера, но не качество реальных моделей. Worktree не оценивался как системный sandbox: проект такой границы не обещает. Гонка продления supervisor lock при долгом tick осталась гипотезой без подтверждённого влияния и не включена в P1.

## Исправления и окончательная проверка

Все восемь подтверждённых P1 исправлены. Для каждой группы сначала наблюдался отказ регрессионного теста, затем успешный результат после исправления. Дополнительных зависимостей и миграций БД нет.

| ID | Исправление | Регрессионные проверки |
|---|---|---|
| R1 | Polling продолжается после EOF живого процесса; завершение leader немедленно останавливает его группу, затем читается накопленный вывод. | [test_provider_eof.py](../tests/unit/test_provider_eof.py): cancel, heartbeat, timeout и child с открытыми pipe. |
| R2 | Явный inspected retry разрешён только для следующего attempt при неизменённом HEAD, своей ветке и подтверждённом владении; права повторно проверяются перед запуском. | [test_runtime_recovery.py](../tests/integration/test_runtime_recovery.py): продолжение частичного патча, отказ при изменении HEAD/pointer/escape и запрет переноса inspection на последующий attempt. |
| R3 | Workflow, отчёт и критерии используют один набор check IDs из текущего done task, сверенный с SQL evidence, SHA, exit code и хешами артефактов. | [test_verification_attempts.py](../tests/integration/test_verification_attempts.py): same-task retry PASS→FAIL, failed/blocked/incomplete attempt, чужой кандидат, повреждённый отчёт и старое test proof. |
| R4 | Повтор current verification возвращает partial run в evaluate и сбрасывает прежний outcome. Новые причины добавляются без потери audited решений по прежним находкам. | [test_runtime_recovery.py](../tests/integration/test_runtime_recovery.py), [test_verification_attempts.py](../tests/integration/test_verification_attempts.py): успешный retry завершается; новая P0 остаётся открытой и блокирует принятие. |
| R5 | Candidate, worktree и phase=verify фиксируются атомарно после Git; постановка проверок идемпотентна. Очередь пропускает superseded tasks; собственный поздний результат освобождает seat, чужой — нет. | [test_runtime_recovery.py](../tests/integration/test_runtime_recovery.py): crash после первой роли, rollback checkpoint, старый pending candidate и собственный/чужой stale finish. |
| R6 | Generic completion разрешён только для team tasks; run tasks требуют валидированного результата провайдера или checks. | [test_runtime_recovery.py](../tests/integration/test_runtime_recovery.py): четыре вида run task отвергаются без изменений; ручное завершение team task сохранено. |
| R7 | Fingerprint причины сохраняет точные detail/evidence, включая регистр и пробелы литералов. | [test_verification_attempts.py](../tests/integration/test_verification_attempts.py): Root/root и значимые пробелы не получают чужое dismissal. Основной агент отдельно повторил RED/GREEN с точечной мутацией прежней нормализации в памяти. |
| R8 | Writer dispatch использует ранее созданные task/ready owned worktree. Постановка task и запись phase атомарны после Git-проверок; повторный model call не выпускается. | [test_runtime_recovery.py](../tests/integration/test_runtime_recovery.py): десять случаев implement/revise, три окна аварии и reuse pending/done task. |

Ревьюер провайдеров независимо перечитал workflow-исправления; ревьюер workflow независимо перечитал runtime-исправления и окончательный writer dispatch. Новых подтверждённых P0/P1 в этих повторных проверках не найдено. Основной агент проверил объединённый diff и запустил общий набор на стабильном коде.

Окончательная проверка на Linux:

- `.venv/bin/pytest -q`: **323 passed, 9 skipped in 143.02 s**. Девять пропусков — явно включаемые реальные Codex/Claude/mixed сценарии. Реальные модели не вызывались.
- `.venv/bin/ruff check .`: **passed**. `git diff --check`: **passed**.
- `.venv/bin/python -m pip wheel --no-deps . --wheel-dir /tmp/agentcrew-review-wheel`: **passed**. Wheel SHA-256: `a9b1cda5e6e04ef85aa24dd10b3836a6c6f9eb88230148161e4571fded3abfa8`.
- Новый wheel установлен в отдельный свежий `/tmp` venv: импорт из `site-packages`, SQLite migration и current-check helper проверены; version, doctor JSON, пустой status JSON, recover preview JSON и help команды completion выполнены успешно.

Один дополнительный промежуточный E2E-запуск был прерван SIGINT после 22 успешных тестов; он не считается полной проверкой. Окончательный общий набор выше завершился полностью и включает эти E2E.

Вердикт по запрошенному исправлению: **подтверждённых открытых P0/P1 нет; шесть P2 перечислены выше и оставлены вне этого изменения**. Это результат локального code review, не подтверждение качества реальных моделей или готовности публичного релиза.
