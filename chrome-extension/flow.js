(() => {
  const pairing = /^#vetay=([a-f0-9]{32})$/.exec(location.hash)?.[1];
  if (pairing) history.replaceState(history.state, "", location.pathname + location.search);
  if (window.__veTayRunning) return;
  window.__veTayRunning = true;
  const sleep = (ms) => new Promise(resolve => setTimeout(resolve,ms));
  const visible = node => node.getClientRects().length && getComputedStyle(node).visibility !== "hidden";
  const send = async (message) => {
    const response = await chrome.runtime.sendMessage(message);
    if (!response?.ok) throw Error(response?.error || "Mất kết nối với Vẽ Tay.");
    return response.result;
  };
  const candidates = () => [...document.images].filter(img => img.complete && img.naturalWidth >= 384 &&
    img.naturalHeight >= 384 && visible(img) && /^(blob:|https:\/\/[^/]*\.googleusercontent\.com\/)/i.test(img.currentSrc));
  const caption = button => (button.getAttribute("aria-label") || button.getAttribute("title") || button.textContent || "").trim();
  async function run() {
    while (true) {
      const job = await send({op:"next"});
      if (job.done) return;
      const fields = [...document.querySelectorAll('textarea,[contenteditable="true"]')].filter(visible);
      if (fields.length !== 1) throw Error("Không xác định được ô prompt duy nhất. Mở trang tạo ảnh trong dự án Flow.");
      const field = fields[0];
      field.focus();
      if (field instanceof HTMLTextAreaElement) {
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,"value").set.call(field,job.prompt);
        field.dispatchEvent(new InputEvent("input",{bubbles:true,data:job.prompt,inputType:"insertText"}));
      } else {
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(field);
        selection.removeAllRanges();
        selection.addRange(range);
        if (!document.execCommand("insertText",false,job.prompt)) throw Error("Không điền được prompt trong Flow.");
      }
      await sleep(500);
      const buttons = [...document.querySelectorAll("button,[role=button]")].filter(b=>visible(b)&&!b.disabled&&b.getAttribute("aria-disabled")!=="true");
      let generate = buttons.filter(b=>/^(Generate Image|Create Image|Tạo hình ảnh|Tạo ảnh)$/i.test(caption(b)));
      if (generate.length === 0 && matching(/Nano Banana|Imagen/i).length === 1)
        generate = buttons.filter(b=>/^(Generate|Tạo)$/i.test(caption(b)));
      if (generate.length !== 1) throw Error("Không nhận diện được nút tạo ảnh. Kiểm tra chế độ Ảnh, model và hạn mức Flow.");
      const before = new Set(candidates().map(i=>i.currentSrc));
      generate[0].click();
      let result, stableSince = 0;
      for (let elapsed=0;elapsed<175;elapsed++) {
        await sleep(1000);
        const found = [...new Set(candidates().map(i=>i.currentSrc).filter(src=>!before.has(src)))];
        if (found.length > 1) throw Error("Flow trả nhiều ảnh. Chọn một kết quả mỗi lượt rồi chạy lại.");
        if (found[0] !== result) { result=found[0]; stableSince=elapsed; }
        if (result && elapsed-stableSince>=3) break;
      }
      if (!result) throw Error("Chưa nhận được ảnh mới từ Flow.");
      let data;
      try {
        const response = await fetch(result);
        if (!response.ok) throw Error("Không tải được ảnh trên tab.");
        const blob = await response.blob();
        if (!blob.type.startsWith("image/") || blob.size > 24*1024*1024) throw Error("Ảnh không hợp lệ.");
        data = await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(blob);});
      } catch (error) { if (result.startsWith("blob:")) throw error; }
      await send({op:"image",job:job.job,index:job.index,url:result,data});
    }
  }
  async function waitFor(find, seconds) {
    for (let i=0;i<seconds*2;i++) {
      const value=find();
      if (value) return value;
      await sleep(500);
    }
    return null;
  }
  const projectOpen = () => /\/projects?\/[^/]+/.test(location.pathname);
  const matching = (pattern) => [...document.querySelectorAll("button,[role=button],[role=tab],a")]
    .filter(item=>visible(item)&&!item.disabled&&item.getAttribute("aria-disabled")!=="true"
      && (pattern.test(caption(item)) || pattern.test((item.innerText || item.textContent || "").replace(/\s+/gu, " ").trim())));
  async function connect() {
    const state = await send(pairing ? {op:"pair",token:pairing} : {op:"resume"});
    if (state.done) return;
    if (document.readyState === "loading") await new Promise(resolve=>document.addEventListener("DOMContentLoaded",resolve,{once:true}));
    if (!projectOpen()) {
      if (!pairing) throw Error("Flow vẫn ở trang chủ sau khi tạo dự án. Kiểm tra đăng nhập rồi chạy lại.");
      const create = await waitFor(() => {
        const choices = matching(/^(?:(?:add(?:_circle|_box)?|plus|\+)\s*)?(?:New project|Create project|Create new project|Dự án mới|Tạo dự án|Tạo dự án mới)$/iu);
        const controls = choices.filter(item => !choices.some(other => other !== item && item.contains(other)));
        return controls[0] || null;
      },35);
      if (!create) throw Error("Không tìm thấy nút tạo dự án Flow. Kiểm tra Chrome đã đăng nhập và xem trang Flow có thông báo gì.");
      create.click();
      if (!await waitFor(projectOpen,45)) throw Error("Flow chưa mở dự án mới sau khi bấm tạo.");
    }
    if (!await waitFor(()=>document.querySelector('textarea,[contenteditable="true"]'),45))
      throw Error("Dự án mới chưa hiện ô prompt.");
    const imageMode=matching(/^(Image|Ảnh|Hình ảnh)$/i);
    if (imageMode.length===1) imageMode[0].click();
    await run();
  }
  connect().catch(async error => {
    try {await send({op:"error",message:error.message});} catch {}
  }).finally(()=>{window.__veTayRunning=false;});
})();