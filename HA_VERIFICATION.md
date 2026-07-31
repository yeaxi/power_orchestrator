# Power Orchestrator — контрольована HA UI verification procedure

Цей документ описує **майбутню контрольовану перевірку** інтеграції у Home Assistant. Він не є дозволом на deployment і не виконує жодних live-дій.

> **Safety boundary:** до окремого явного дозволу не виконувати deploy, `reload`, `restart`, запис у live config entry або будь-які фізичні `turn_on`/`turn_off` service calls. Локальні тести інтеграції використовують mocks.

## 1. Мета та критерій успіху

Перевірити в UI Home Assistant:

- HACS/package installation та manifest metadata;
- config flow і optional Energy Dashboard discovery;
- підтвердження/видалення discovered candidates;
- додавання custom devices і on/off control mapping;
- friendly names та named priority selectors;
- runtime entities і stable unique IDs;
- режими `auto`/`off`;
- fail-closed behavior для grid/battery/load/forecast inputs;
- one-device-per-cycle normal control;
- emergency stop, pause persistence і manual-override notification.

Успіх означає, що всі обов'язкові перевірки нижче пройдені, кожна фізична дія має очікуваний readback, а жоден небезпечний або невизначений input не спричиняє normal start.

## 2. Передумови та approval gates

### Обов'язково до live-сесії

- [ ] Є explicit approval на окрему live UI verification session.
- [ ] Обрано лише не критичні/test devices або підготовлено безпечне вікно.
- [ ] Зафіксовано поточні entity IDs, automations і попередню конфігурацію.
- [ ] Визначено оператора, який може фізично вимкнути навантаження вручну.
- [ ] Відомо, як повернути попередню версію інтеграції або видалити її config entry.
- [ ] Для safety тестів доступні test sensors/fixtures або контрольовані helper entities.

### Заборонено в межах цієї процедури без окремого дозволу

- [ ] `ha core restart`, reload integration або reload config entry.
- [ ] Увімкнення реальних бойлерів/акумулятора як тестовий крок.
- [ ] Зміна live Energy Dashboard, Forecast.Solar або батарейних sensors.
- [ ] Ручне редагування `.storage` під час запущеного HA.
- [ ] Видалення старих automations/packages до завершення паралельного спостереження.

## 3. Installation/package check

Виконувати лише після approval на installation, не підміняти цим локальні тести.

1. Відкрити **Settings → Devices & services → HACS**.
2. Знайти `Power Orchestrator` або додати repository як custom integration.
3. Перевірити, що package має version `0.5.0` і документацію на repository URL.
4. Встановити integration, але не запускати фізичне керування.
5. Переконатися, що integration manifest не містить credentials або connection strings.

**Очікування:** HACS приймає package layout; integration доступна у **Add Integration**; version і domain `power_orchestrator` відповідають package metadata.

## 4. Config flow walkthrough

### 4.1 Auto-Discovery

Відкрити **Settings → Devices & services → Add Integration → Power Orchestrator**.

Перевірити:

- Energy Dashboard є optional prerequisite;
- відсутній Energy Dashboard не блокує custom-device path;
- `device_consumption.stat_consumption` показується як candidate identity;
- `stat_rate` використовується лише як optional power telemetry;
- dashboard `name` зберігається як friendly name;
- якщо dashboard name відсутній, UI показує HA `friendly_name`, а потім entity ID;
- Forecast.Solar selector показує лише config entries integration `forecast_solar`.

**Очікування:** discovery не активує жодного пристрою автоматично. Якщо entry вже існує, повторне додавання завершується abort `single_instance`; second entry не створюється.

### 4.2 Load Monitoring

Вказати load sensor і перевірити поля:

- Load sensor;
- Maximum total load (W);
- Averaging period (s);
- Safety reserve (W);
- Hysteresis (W).

**Очікування:** load sensor є обов'язковим. Значення невалідного або stale sensor у runtime не перетворюється на `0 W` і не дозволяє start.

### 4.3 Optional Devices

Для discovered candidates:

1. Переконатися, що всі candidates за замовчуванням selected.
2. Видалити один candidate з multi-select.
3. Перевірити, що видалений candidate не переходить у наступний control step.
4. Для залишеного candidate вибрати окрему controllable entity у домені `switch`, `light` або `input_boolean`.
5. Перевірити prefilled power sensor з Energy Dashboard `stat_rate`.
6. Замінити його на інший `sensor` entity і переконатися, що custom value зберігається; окремо перевірити, що очищення поля вимикає measured telemetry.
7. Вказати friendly name, expected power і прапорець `only_from_solar`.

Для custom device:

1. Обрати **Add a custom device**.
2. Вказати on/off entity окремо від Energy Dashboard statistics entity.
3. Вибрати власний power sensor або залишити його порожнім, якщо measured telemetry не потрібна.
4. Додати другий custom device через **Add another**.
5. Перевірити, що обидва пристрої збереглися.

**Очікування:** statistics sensor не використовується як physical control entity; жоден пристрій не вмикається під час config flow.

### 4.4 Priority & Pause

Перевірити named selectors:

- `Priority position 1`, `Priority position 2`, …;
- option labels — friendly names, не raw entity IDs;
- кожен device обраний рівно один раз;
- duplicate або unknown selection повертає localized validation error;
- pause period збережений у секундах.

**Очікування:** position 1 — найвищий priority; останній у списку вимикається першим під час normal load shedding.

### 4.5 Grid Loss Behavior

Перевірити два взаємовиключні safety modes:

#### Sensor mode

- `Detection mode = Grid loss sensor`;
- grid-loss binary sensor обов'язковий;
- `on` означає grid available;
- `off`, `unknown`, `unavailable`, missing або stale означають unsafe/grid-loss.

#### Battery threshold mode

- `Detection mode = Battery threshold`;
- battery SoC sensor обов'язковий;
- threshold finite і в діапазоні `0..100`;
- SoC має бути **строго вище** threshold для normal start.

**Очікування:** відсутній required source повертає form error, а не створює fail-open entry.

## 5. Entity and device verification

Після створення entry перевірити один device `Power Orchestrator` і такі entities:

| Entity role | Expected stable unique-ID suffix |
|---|---|
| Status sensor | `_sensor_status` |
| Current load | `_sensor_current_load` |
| Average load | `_sensor_average_load` |
| Available capacity | `_sensor_available_capacity` |
| Last action | `_sensor_last_action` |
| Grid OK | `_binary_sensor_grid_ok` |
| Mode selector | `_select_mode` |

Повний unique ID має бути scoped до config-entry ID, наприклад `<entry_id>_sensor_status`.

Перевірити:

- sensor names: Status, Current load, Average load, Available capacity, Last action;
- binary sensor: Grid OK;
- select options: `auto`, `off`;
- device info model `v0.5.0` відповідає manifest version `0.5.0`;
- всі entities належать правильному одному integration device;
- entity registry не створила `_2`/ghost duplicates.
- newly created config entry starts in `off` before the first coordinator refresh;
- only an explicitly persisted `auto` mode may restore automatic starts after a later reload/restart.

## 6. Safe runtime checks

Усі runtime checks виконувати спочатку на mocks/helpers або не критичному test load. Після кожного кроку перевіряти status sensor, Last action і фізичний relay state.

| Test input/event | Expected result | Forbidden result |
|---|---|---|
| fresh valid grid `on`, fresh valid load, device `off` | normal evaluation may consider highest-priority device | batch start of multiple devices |
| device state `unknown`/`unavailable` | no normal start; state remains unknown/safety blocked as appropriate | treating unknown as `off` |
| load `unknown`, `unavailable`, NaN, negative, wrong unit or stale | `safety_blocked`; invalid sample not added as `0 W` | normal start |
| grid sensor `off`/missing/stale | emergency stop path; no start | leaving known-on managed load running without stop attempt |
| battery SoC at/below threshold, invalid or stale | emergency stop/grid-loss behavior | normal start |
| fresh Forecast.Solar `power_production_now` below expected power | solar-only device remains off | fallback to actual PV or unrelated forecast sensor |
| Forecast unavailable, stale, future, wrong unit, prior clock hour | solar-only device remains off | forecast-only admission |
| valid load above max | one lowest-priority known-on device is shed | batch shedding of all devices |
| mode `off` | no normal physical starts | start due to capacity |
| mode `off` plus grid loss | emergency stop still active | `off` disabling emergency safety |
| service call raises or relay readback fails | state becomes unknown; `safety_blocked`; no success claim | assuming service call succeeded |

Normal start/stop actions are one per evaluation cycle. Emergency all-stop is the intentional exception.

## 7. Pause, restart and options lifecycle

With a controlled/test device:

1. Cause a normal load-shedding stop.
2. Verify pause timestamp is set and device is not immediately restarted.
3. Change options through the Options UI.
4. Verify config update triggers the expected reload only after the separate reload approval.
5. Verify mode and bounded pause state are restored after an approved restart test.
6. Verify corrupt, expired, future, non-finite or overlong persisted pause values are ignored/fail-safe.

Do not edit HA storage files while HA is running. If registry cleanup is required, stop HA using the operator-approved procedure first, then make the cleanup and start it again only with separate approval.

## 8. Services and manual override

After an explicit service-test approval:

- call `power_orchestrator.set_mode` with `auto` and `off` only;
- call `power_orchestrator.set_mode` without `mode` and with an invalid value; both must be rejected before the handler can arm automatic mode;
- verify a second config entry is refused and a forced evaluation cannot start one device per entry;
- call `power_orchestrator.force_evaluate` and verify immediate entity update;
- after a delayed or missing relay readback, verify a compensating `turn_off`; if OFF is not confirmed, the device remains unknown and status is `safety_blocked`;
- manually re-enable a device during an active emergency-stop episode;
- verify one persistent notification with entry/device-specific ID:
  `power_orchestrator_<entry_id>_<device_id>_manual_override`;
- verify repeated evaluations do not create duplicate notifications for the same episode.

After the last entry is unloaded, verify integration services are removed from the service registry.

## 9. Rollback

Stop immediately and roll back if any of the following occurs:

- unknown/stale safety input causes a normal start;
- relay readback does not match the requested state but integration reports success;
- more than one normal action occurs in one evaluation cycle;
- `off` mode starts a device;
- emergency stop fails without `safety_blocked` reporting;
- options flow accepts a required-source omission;
- entity IDs collide or duplicate entities appear;
- service callbacks remain after entry unload.

Rollback sequence, only with explicit approval:

1. Set integration mode to `off`.
2. Stop the integration/config entry and manually verify managed loads are safe.
3. Restore the previously recorded config/automation path.
4. Remove the integration entry or install the previous known-good package version.
5. Reload/restart Home Assistant only under the approved operational procedure.
6. Record the observed failure, status sensor value, Last action, entity ID, and timestamps without including credentials.

## 10. Evidence record

For each approved live verification session record:

- Home Assistant version;
- integration/package version;
- config-entry ID (non-secret);
- selected load and safety source entity IDs;
- configured devices, friendly names, priorities and expected powers;
- entity unique IDs;
- test case, timestamp, expected result and observed result;
- whether any physical action occurred;
- rollback decision and operator approval.

Never include passwords, API keys, tokens, cookies, connection strings or private credentials in the record.

## 11. Current local verification status

The local, non-live quality gate currently covers:

- mocked full regression suite;
- Python compilation;
- JSON resource validation;
- YAML parsing for `services.yaml` and CI workflow;
- config/options flow validation;
- safety, freshness, readback, persistence, one-device-per-cycle and service lifecycle tests.

A local pass is not a substitute for the controlled live HA verification above.
