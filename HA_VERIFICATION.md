# Power Orchestrator — контрольована HA verification procedure

Цей документ описує **майбутню контрольовану перевірку** інтеграції. Він не є дозволом на deployment і не виконує live-дій.

> **Safety boundary:** без окремого явного дозволу не виконувати deploy, reload, restart, запис у live config/storage або будь-які фізичні `turn_off` service calls. Локальні тести використовують mocks.

## 1. Мета та критерій успіху

Перевірити, що інтеграція працює **лише як load-shedding controller**:

- конфігурація приймає aggregate load, safety source та optional loads;
- `auto`/`off` і `observe`/`live` мають чіткі межі;
- перевищення ліміту виконує bounded stop з readback;
- аварійний grid/battery стан виконує all-stop path;
- unknown/stale/invalid input не authorizes фізичну дію;
- після restart валідний persisted mode відновлюється без безумовного скидання;
- немає PV/forecast admission, normal enable або automatic re-enable surface.

В integration немає **no normal automatic enabling**: її фізична action surface stop-only.

Успіх означає, що всі обов'язкові перевірки нижче пройдені, кожна дозволена фізична дія має очікуваний readback, а жоден небезпечний або невизначений input не призводить до normal action.

## 2. Передумови та approval gates

### Обов'язково до live-сесії

- [ ] Є explicit approval на окрему live UI verification session.
- [ ] Обрано лише non-critical/test loads або підготовлено безпечне вікно.
- [ ] Зафіксовано entity IDs, automations і попередню конфігурацію.
- [ ] Визначено оператора, який може фізично вимкнути навантаження вручну.
- [ ] Є rollback procedure.
- [ ] Для safety тестів доступні test sensors/helpers.

### Заборонено без окремого дозволу

- [ ] `ha core restart`, reload integration або reload config entry.
- [ ] Зміна live dashboard, battery sensors або фізичних навантажень.
- [ ] Ручне редагування `.storage` під час запущеного HA.
- [ ] Видалення старих automations/packages до завершення спостереження.

## 3. Installation/package check

Виконувати лише після approval на installation; це не замінює локальні тести.

1. Перевірити manifest, version, domain і package layout.
2. Встановити integration, не активуючи фізичне керування.
3. Переконатися, що package не містить credentials, tokens або connection strings.
4. Залишити planner mode `off`, execution mode `observe`.

**Очікування:** package приймається Home Assistant/HACS, config flow доступний, фізичні service calls не виконуються під час installation.

## 4. Config flow walkthrough

Перевірити:

- Energy Dashboard discovery є optional і не є safety permit;
- aggregate load sensor є обов'язковим;
- safety source можна налаштувати як grid sensor або battery threshold;
- custom load має controllable entity, expected power, optional power sensor і actuator group;
- priority/pause поля мають inline descriptions;
- відсутні PV, forecast, generation, normal-enable або automatic-re-enable поля;
- duplicate/invalid devices і missing safety source відхиляються.

**Очікування:** config flow не змінює state жодного навантаження.

## 5. Entity and device verification

Після створення entry перевірити один device **Power Orchestrator** і config-entry-scoped unique IDs для:

- status;
- current/average load;
- available capacity;
- last action/operation;
- execution mode/reason code;
- Grid OK;
- Faulted;
- Action journal healthy;
- mode select з опціями `auto`, `off`.

Перевірити, що новий entry є `off` до першої evaluation, а лише валідний persisted mode може відновити `auto` після reload/restart.

## 6. Safe runtime checks

Спочатку використовувати mocks/helpers або non-critical test load. Після кожного кроку перевіряти status, reason code, last action і фактичний actuator state.

| Input/event | Очікуваний результат | Заборонений результат |
|---|---|---|
| Fresh valid grid `on`, valid load | Безпечна evaluation без фізичної дії | Нормальне automatic enabling |
| Load `unknown`, unavailable, NaN, negative, wrong unit або stale | `safety_blocked`; sample не стає `0 W` | Дозволена дія |
| Grid `off`, missing або stale | Emergency stop path; bounded stop attempt | Залишити відомий активний load без stop attempt |
| Battery SoC at/below threshold або stale | Grid-loss/safety behavior | Дозволена normal дія |
| Valid load above limit | Один lowest-priority known-on load shed | Batch shedding або re-enable |
| Mode `off` | Немає ordinary physical action | Mode bypass |
| Mode `off` + emergency state | Emergency handling залишається активним | `off` вимикає safety stop |
| Service error/readback failure | Unknown/faulted/safety-blocked state | Claim success без readback |

Нормальний stop — не більше одного за evaluation cycle. Emergency all-stop є окремим дозволеним винятком.

## 7. Pause, restart and options lifecycle

У controlled/test environment:

1. Створити overload і перевірити bounded stop.
2. Перевірити pause timestamp та відсутність будь-якого automatic re-enable.
3. Встановити mode `auto`, переконатися, що storage записав його.
4. Виконати окремо approved restart test.
5. Перевірити, що mode `auto` відновився до першої evaluation і не був скинутий у `off` без причини.
6. Перевірити, що missing/corrupt/invalid persisted data дає safe `off`.
7. Перевірити Options/Reconfigure і guarded reload.

Не редагувати HA storage вручну під час запущеного HA.

## 8. Services and manual override

Після окремого service-test approval:

- перевірити `set_mode` лише для `auto`/`off`;
- перевірити reject missing/invalid mode до виконання handler;
- викликати `force_evaluate` і перевірити entity update;
- викликати `request_stop` для відомого device;
- перевірити, що readback failure залишає device unknown/faulted;
- перевірити `clear_quarantine` лише після незалежних fresh OFF/load/readback доказів;
- перевірити відсутність будь-якого service, що додає або запускає навантаження;
- після unload перевірити, що integration services видалені з registry.

## 9. Rollback

Зупинити перевірку і виконати rollback, якщо:

- unknown/stale input призводить до звичайної physical action;
- readback не відповідає command, але integration повідомляє success;
- більше одного ordinary action відбувається за цикл;
- `off` обходиться;
- emergency stop не створює safety-blocked state;
- після unload залишаються listeners/services.

Rollback sequence лише з approval:

1. Встановити mode `off`.
2. Зупинити config entry та незалежно перевірити фізичні loads.
3. Відновити попередній відомий пакет/config path.
4. Reload/restart виконати лише за approved operational procedure.
5. Зафіксувати status, reason, action, entity ID і timestamp без credentials.

## 10. Evidence record

Для кожної approved session записати:

- Home Assistant та integration version;
- config-entry ID;
- load/safety source entity IDs;
- configured loads, names, priorities, expected powers;
- entity unique IDs;
- test case, timestamp, expected/observed result;
- чи відбулася physical дія;
- rollback decision та approval.

Ніколи не включати passwords, API keys, tokens, cookies, connection strings або private credentials.

## 11. Current local verification status

Локальний non-live gate має покривати:

- повний mocked regression suite;
- Python compilation;
- JSON resource validation;
- YAML parsing для `services.yaml` і CI workflow;
- config/options flow;
- safety, freshness, readback, mode persistence та service lifecycle;
- static scan, який забороняє PV/forecast/admission/normal-enable surface.

Цей документ описує майбутню перевірку і **not a substitute for the controlled live HA verification**.
