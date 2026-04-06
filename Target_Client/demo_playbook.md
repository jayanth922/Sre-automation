# 🎭 The SRE Agent Demonstration Playbook

This document is your master script for presenting the **Multi-Agent SRE Assistant** (CMPE295A). It explains why this sandbox exists, how the dashboard visuals map to your actions, and exactly how to use it to prove your AI Agent's value to a grading committee or audience.

---

## 1. The Cause and Effect (What to Expect)

Your dashboard is built entirely around Google's famous **"Four Golden Signals"** of monitoring: Traffic, Errors, Latency, and Saturation (business metrics). 

Here is exactly what happens when you touch the controls:

| The Control You Move | The Immediate Technical Effect | What You See on the Dashboard |
| :--- | :--- | :--- |
| **Checkout Error Rate** | The checkout microservice starts randomly rejecting payments with `HTTP 500` codes. | 🔴 The red **HTTP Error Rate** graph will smoothly rise up to match the percentage you chose. The purple **Payment Failures** graph will also spike. |
| **Checkout Slow Rate** &<br>**Inventory Slow Query** | The code inserts artificial `asyncio.sleep()` delays (up to 3.5 seconds) in the backend. | 🟡 The yellow **Request Latency (p95)** graph will sharply rise from ~0.05s up into the 2.0s - 3.5s range. |
| **Chaos Mode Toggle** | Simulates a catastrophic Database crash. 50% of all checkout attempts instantly fail. | 🧨 **Massive spike across the board.** The red Error, yellow Latency, and purple Payment graphs will all spike simultaneously. Red Alerts will instantly fire. |
| **Trigger Burst** | The Load Generator immediately blasts the API Gateway with your configured Burst RPS. | 🔵 The blue **Throughput** graph goes straight up. If your other sliders are up, the sheer volume of traffic will multiply the size of the errors/latency spikes. |

> **Should you add more graphs?** 
> **No.** In presentation design, less is more. Adding CPU or Memory graphs will clutter the UI and distract the audience. The 4 graphs you have now are the exact metrics a real-world executive cares about: "Are we up? Is it fast? Is traffic flowing? Are payments failing?"

---

## 2. Why does this prove the SRE Agent's worth?

If you just showed a professor a terminal where an AI prints out "The server is down," they wouldn't be impressed. It looks faked. 

The **Chaos Control Panel** is the ultimate proof because it forces the AI to work in a live, hostile, and unpredictable environment. It proves your agent isn't just reading static text files; it's actively analyzing real-world telemetry in real-time.

To prove the agent works, you demonstrate the **Human vs. Machine** workflow.

### The Master Demo Script

Follow these steps during your presentation to guarantee a "WOW" factor:

#### Phase 1: The Setup (Normal Operations)
1. Keep all sliders at **0%**. Set Load RPS to **5**.
2. Show the audience the Chaos Panel. 
   * *"Notice the system is healthy. The graphs are flat, latency is microscopic, and there are no Prometheus alerts."*

#### Phase 2: The Attack (Injecting Chaos)
1. Slide the **Inventory Slow Query Rate to 80%**.
2. Hit **Trigger Burst Now**.
3. Point to the dashboard as it lights up like a Christmas tree. 
   * *"We just got hit by a traffic spike while our Inventory Database degraded. The yellow Latency graph is spiking to 3 seconds, and Prometheus just triggered a critical 'InventorySlowQueries' alert."*

#### Phase 3: The AI Resolution (Proving Value)
1. Switch your screen to your **SRE Agent Interface** (or terminal).
2. Wake the agent up and say: *"Hey, Prometheus just fired an alert. What's going on with the system?"*
3. **The Magic Moment:** The audience will watch the AI do exactly what a human $150k/year Site Reliability Engineer would do:
   * **Step 1:** The AI will autonomously query Prometheus and see the 3-second latency.
   * **Step 2:** It will autonomously query Promtail/Loki for logs to see *why* it's slow.
   * **Step 3:** It will read the Python logs and output: *"I found the root cause. The `inventory-service` is experiencing severe database query delays (duration_ms=2500) causing cascading latency across the API Gateway."*

#### Phase 4: Resolution
1. Go back to the Chaos Panel manually drop the Slow Query Rate back to **0%**.
2. Point out how the red alerts clear to green, and the graphs drop back down to normal.
3. Conclude: *"Instead of taking 45 minutes for a human engineer to run queries and find the database bottleneck, the Multi-Agent system diagnosed the root cause in 10 seconds."*
