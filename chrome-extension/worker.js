const base = "http://127.0.0.1:7877";
function flowTab(url) { return /^https:\/\/(flow\.google\.com|labs\.google)\//.test(url || ""); }
async function api(token, path, body) {
  const r = await fetch(base + path, {
    headers: {"X-VeTay-Token":token, ...(body ? {"Content-Type":body.type || "application/json"} : {})},
    method: body ? "POST" : "GET", body
  });
  if (!r.ok) throw Error((await r.text()).slice(0,250));
  return r.json();
}
chrome.runtime.onMessage.addListener((m,sender,reply) => {
  (async()=>{
    if (!flowTab(sender.tab?.url)) throw Error("Chỉ nhận yêu cầu từ tab Flow.");
    const key = "pairing:" + sender.tab.id;
    if (m.op === "pair") {
      if (!/^[a-f0-9]{32}$/.test(m.token || "")) throw Error("Mã kết nối không hợp lệ.");
      const job = await api(m.token, "/next");
      if (job.done) throw Error("Vẽ Tay chưa có cảnh chờ.");
      await chrome.storage.session.set({[key]:m.token});
      return job;
    }
    const saved = await chrome.storage.session.get(key);
    const pairing = saved[key];
    if (m.op === "resume") return pairing ? api(pairing,"/next") : {done:true};
    if (!pairing) throw Error("Tab chưa được Vẽ Tay kết nối.");
    if (m.op === "activate") {
      await chrome.tabs.update(sender.tab.id, {active:true});
      await chrome.windows.update(sender.tab.windowId, {focused:true});
      return {active:true};
    }
    if (m.op === "next") return api(pairing,"/next");
    if (m.op === "error") return api(pairing,"/error",JSON.stringify({message:m.message}));
    if (m.op === "image" && /^[a-f0-9]{32}$/.test(m.job) && Number.isInteger(m.index)) {
      let image;
      if (m.data && /^data:image\/(png|webp|jpeg);base64,/.test(m.data)) {
        image = await (await fetch(m.data)).blob();
      } else {
        const u = new URL(m.url);
        if (u.protocol !== "https:" || !/(^|\.)googleusercontent\.com$/.test(u.hostname)) throw Error("Không xác định được địa chỉ ảnh kết quả.");
        const r = await fetch(u.href,{credentials:"omit",redirect:"error"});
        if (!r.ok) throw Error("Không tải được ảnh kết quả.");
        image = await r.blob();
      }
      if (image.size > 24*1024*1024 || !image.type.startsWith("image/")) throw Error("Ảnh kết quả không hợp lệ.");
      return api(pairing,"/image/"+m.job+"/"+m.index,image);
    }
    throw Error("Yêu cầu không hợp lệ.");
  })().then(result=>reply({ok:true,result}),error=>reply({ok:false,error:error.message}));
  return true;
});
chrome.tabs.onRemoved.addListener(tabId => chrome.storage.session.remove("pairing:" + tabId));

const extensionCheck = "vetay-check-local-update";
function registerCheck() {
  chrome.alarms.create(extensionCheck, {periodInMinutes:1});
}
chrome.runtime.onInstalled.addListener(registerCheck);
chrome.runtime.onStartup.addListener(registerCheck);
chrome.alarms.onAlarm.addListener(async alarm => {
  if (alarm.name !== extensionCheck) return;
  try {
    const current = chrome.runtime.getManifest();
    const response = await fetch(chrome.runtime.getURL("manifest.json") + "?t=" + Date.now(), {cache:"no-store"});
    if (!response.ok) return;
    const disk = await response.json();
    if (!/^\d+\.\d+\.\d+$/.test(disk.version) || disk.name !== current.name ||
        disk.manifest_version !== current.manifest_version) return;
    const old = current.version.split(".").map(Number);
    const next = disk.version.split(".").map(Number);
    const changed = next.findIndex((value,i)=>value!==old[i]);
    if (changed < 0 || next[changed] < old[changed]) return;
    for(const key of ["permissions","host_permissions","optional_permissions","optional_host_permissions"]) {
      if(JSON.stringify([...(current[key]||[])].sort())!==JSON.stringify([...(disk[key]||[])].sort())) return;
    }
    const matches = value => (value.content_scripts || []).flatMap(script=>script.matches || []).sort();
    if(JSON.stringify(matches(current))!==JSON.stringify(matches(disk))) return;
    const idle = await fetch(base+"/extension-idle",{cache:"no-store"});
    if(idle.ok && (await idle.json()).idle === true) chrome.runtime.reload();
  } catch {}
});
