# Timberbot States Analysis: Automation, Priorities, and Physical Settings

When interacting with Timberbot, AI agents frequently confuse different uses of the terms "High", "Low", and "State". This confusion occurs because the game uses similar terminology across three entirely separate systems: **Work Priorities**, **Automation Signals**, and **Physical Attributes**. 

This report clarifies these systems and their impacts so that you and your agents can reliably manage the colony.

---

## 1. Automation States (`On` / `Off`)

Automation elements (Sensors, Relays, Memory, Levers) communicate using **Boolean states**, not analog "High" or "Low" values. 

*   **The Signal:** A wire is either `On` (True) or `Off` (False).
*   **Sensors (Depth, Flow, Contamination):** These generate a signal by comparing a physical value against a `threshold` using a `mode` (e.g., `Greater`, `LessOrEqual`). If the condition is met, the sensor outputs `On`. Otherwise, it outputs `Off`.
*   **Logic Components (Relays, Memory):** These take `On`/`Off` inputs and apply logic (like `And`, `Or`, `SetReset`) to determine their own `On`/`Off` output.
*   **Building Impacts (polarity):** Treat the signal as **permission to run**.
    *   Input `On` → the building **runs** normally.
    *   Input `Off` → the building is **paused** by automation.
    *   Input *Disconnected* (no wire at all) → the building **runs** normally. Disconnected is not the same as `Off`.
    *   Floodgates are the exception: an automation signal toggles them between pre-configured heights rather than gating their operation.

> [!WARNING]
> Agents should **never** attempt to set an automation wire to "High" or "Low". If configuring a sensor, they must configure the float `threshold` and the comparison `mode`.

## 2. Priority Settings (`VeryLow` to `VeryHigh`)

"High" and "Low" strictly apply to **Priority settings**, which dictate beaver AI pathing and job assignment. They do **not** transmit logic signals.

*   **Workplace Priority (`workplacePriority`):** Determines which buildings get staffed first when there is a worker shortage.
*   **Construction Priority (`constructionPriority`):** Determines which buildings builders construct first.
*   **Valid Values:** Priorities use a specific string enum: `"VeryLow"`, `"Low"`, `"Normal"`, `"High"`, `"VeryHigh"`.

> [!IMPORTANT]
> A common agent hallucination is trying to use `set_priority priority:High` to activate a machine or change a floodgate height. Priorities only affect worker allocation.

## 3. Physical States (Floats / Integers)

Physical mechanics use explicit numerical values rather than descriptive states.

*   **Floodgates (`height`):** Set via `height:X` (e.g., `0.0`, `0.5`, `1.5`, up to the gate's `maxHeight`). Agents often hallucinate commands like `set_floodgate height:Low` instead of `set_floodgate height:0.5`.
*   **Water Depth / Flow:** Read as continuous float values (e.g., `0.85` depth).
*   **Storage Modes:** Set via specific string enums: `"accept"`, `"obtain"`, `"supply"`, or `"empty"`. 

## Summary of Agent Pitfalls to Avoid

1.  **"High/Low" overlap:** An agent might see a floodgate and try to set it to "Low", confusing the `priority` enum with a physical `height` float.
2.  **Automation vs Priority:** An agent might try to link a sensor to a building to set its "Priority to High", when automation wires only transmit `On`/`Off` signals (usually resulting in pausing/unpausing the building).
3.  **Threshold confusion:** When reading a Depth Sensor, an agent might see a float like `0.5` and think the signal is analog. The sensor reading is analog, but its *output* to the wire is strictly `On` or `Off` based on the threshold evaluation.

### Recommendation for Agent Prompting
To prevent this, ensure your agent prompts explicitly enforce:
* *"Use `On`/`Off` for automation logic."*
* *"Use exact floats (e.g., `0.5`, `1.0`) for floodgate heights and sensor thresholds."*
* *"Only use `VeryLow`...`VeryHigh` for beaver work allocation via the priority endpoints."*
