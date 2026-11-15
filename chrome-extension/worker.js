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
    if (m.op === "begin" && !sender.tab) {
      const tab = await chrome.tabs.get(m.tabId);
      if (!flowTab(tab.url)) throw Error("Tab hiện tại không phải Flow.");
      const job = await api(m.token,"/next");
      if (job.done) throw Error("Bấm Tự tạo video trong Vẽ Tay trước.");
      await chrome.storage.session.set({pairing:m.token});
      await chrome.scripting.executeScript({target:{tabId:tab.id},files:["flow.js"]});
      return {};
    }
    if (!flowTab(sender.tab?.url)) throw Error("Chỉ nhận yêu cầu từ tab Flow.");
    const {pairing} = await chrome.storage.session.get("pairing");
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