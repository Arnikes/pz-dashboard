import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_editors import editing, env  # noqa: F401

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.getenv("PZ_UI_PERF") != "1", reason="Opt-in 60-second render measurement: PZ_UI_PERF=1"
    ),
]


def test_render_measurement(page, dashboard, editing):  # noqa: F811 (imported fixture)
    page.set_viewport_size({"width": 390, "height": 844})
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
    cdp.send("Performance.enable")
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    page.evaluate(r"""() => {
      liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer);
      const overview = structuredClone(S.overview);
      const players = {ok:true,names:['Администратор','Игрок'],count:2};
      const backups = {ok:true,items:Array.from({length:200},(_,i)=>({name:'world-long-backup-'+i+'.tar',mtime:'2026-09-01T00:00:00Z',size:50000000})),journal:[]};
      const events = {ok:true,items:Array.from({length:100},(_,i)=>({ts:'2026-09-01T00:00:00Z',type:'backup',text:'Архив '+i+' создан'}))};
      const logs = {ok:true,text:Array.from({length:5000},(_,i)=>'2026-09-01T00:00:00Z INFO Строка журнала '+i).join('\n')};
      window.renderSnapshot = () => {const start=performance.now();renderOverview(overview);renderPlayers(players);renderBackups(backups);renderEvents(events);renderLogs(logs);ConfigEditor.operationChanged();return performance.now()-start;};
      renderSnapshot();
      window.measure = {renders:[],mutations:{},longTasks:[],frames:[],started:performance.now()};
      for (const id of ['playersBody','backupsBody','eventsBody','recentBody','logsOut','bkJournal','editorAttention','configFields','draftBar']) {
        const node=document.getElementById(id);if(!node)continue;measure.mutations[id]=0;
        new MutationObserver(records=>measure.mutations[id]+=records.length).observe(node,{subtree:true,attributes:true,childList:true,characterData:true});
      }
      new PerformanceObserver(list=>measure.longTasks.push(...list.getEntries().map(e=>e.duration))).observe({type:'longtask',buffered:false});
      let last=performance.now();const frame=now=>{measure.frames.push(now-last);last=now;if(!measure.done)requestAnimationFrame(frame);};requestAnimationFrame(frame);
      let count=0;window.measureTimer=setInterval(()=>{measure.renders.push(renderSnapshot());if(++count===60){clearInterval(measureTimer);measure.done=true;measure.elapsed=performance.now()-measure.started;}},1000);
    }""")
    before = cdp.send("Performance.getMetrics")
    page.wait_for_function("measure.done === true", timeout=75000)
    after = cdp.send("Performance.getMetrics")
    result = page.evaluate("measure")
    result.update(
        viewport={"width": 390, "height": 844},
        cpuThrottle=4,
        scenario="60 identical synthetic snapshots; real isolated draft editor; stream stopped for render isolation; 200 backups, 100 events, 5000 log lines",
        before=before,
        after=after,
    )
    target = Path(__file__).resolve().parents[2] / ".tmp-ui-performance" / "metrics.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        json.dumps(
            {
                "file": str(target),
                "elapsedMs": result["elapsed"],
                "renders": len(result["renders"]),
                "mutations": result["mutations"],
                "maxRenderMs": max(result["renders"]),
                "longTasks": len(result["longTasks"]),
            }
        )
    )
