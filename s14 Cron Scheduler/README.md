"""
执行顺序
1、加载持久化任务，load_durable_jobs。（项目启动时
2、启动守护进程，cron_scheduler_loop。（加载任务后，同在cron_scheduler文件中进行
3、启动queue_processor_loop进程，每 0.2 秒检查队列，空闲时唤醒 agent
4、主线程进入 input() 阻塞，等待用户输入。

调度线程cron_scheduler_loop
┌─────────────────────────────────────────────┐
│ 1. sleep(1)                                  │
│ 2. now = datetime.now()                      │
│ 3. minute_marker = "YYYY-MM-DD HH:MM"        │
│ 4. 遍历 scheduled_jobs 副本：                 │
│    ├─ cron_matches(job.cron, now)?           │
│    │   ├─ 是 → 检查 _last_fired 是否已触发    │
│    │   │       ├─ 未触发 → cron_queue.append  │
│    │   │       │            _last_fired=marker│
│    │   │       └─ 已触发 → 跳过              │
│    │   └─ 否 → 跳过                          │
│    └─ 非 recurring → 从 scheduled_jobs 移除   │
│                       durable → 立即持久化    │
└─────────────────────────────────────────────┘

队列处理进程queue_processor_loop
┌─────────────────────────────────────────────┐
│ 1. sleep(0.2)                                │
│ 2. has_cron_queue()?                         │
│    └─ 否 → continue                          │
│ 3. agent_lock.acquire(blocking=False)?       │
│    └─ 失败 → continue（agent 忙）            │
│ 4. 再次 has_cron_queue()?                    │
│    └─ 否 → 释放锁, continue（双重检查）       │
│ 5. run_agent_turn_locked()   ← 唤醒 agent    │
│ 6. agent_lock.release()                      │
└─────────────────────────────────────────────┘
"""

"""
cron_scheduler
整体架构是经典的生产者-消费者模型：
┌──────────────────┐        ┌────────────┐        ┌──────────────────┐
│ cron_scheduler   │  写    │ cron_queue │  读    │ queue_processor  │
│ _loop (生产者)   │ ─────► │ (缓冲区)   │ ─────► │ _loop (消费者)   │
│ 每秒轮询         │        │ 线程安全   │        │ 每0.2秒轮询      │
└──────────────────┘        └────────────┘        └──────────────────┘
                                                          │
                                                          │ 获取 agent_lock
                                                          ▼
                                                   ┌──────────────────┐
                                                   │  agent_loop      │
                                                   │  (实际执行)      │
                                                   │  消费队列 + API  │
                                                   └──────────────────┘
                                                          ▲
                                                          │ 获取 agent_lock
                                                   ┌──────────────────┐
                                                   │  主线程 (用户)   │
                                                   └──────────────────┘
"""