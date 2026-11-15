(() => {
  const pairing = /^#vetay=([a-f0-9]{32})$/.exec(location.hash)?.[1];
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
  function promptField() {
    const fields = [...document.querySelectorAll('textarea,[contenteditable="true"],[contenteditable="plaintext-only"]')]
      .filter(field => visible(field) && !field.closest('[inert],[aria-hidden="true"],[role="search"]')
        && !field.disabled && !field.readOnly && field.getAttribute("aria-disabled") !== "true");
    const labels = field => ["aria-label", "placeholder", "data-placeholder", "title"]
      .map(name => field.getAttribute(name) || "").join(" ");
    const eligible = fields.filter(field => !/search|tìm kiếm|rename|đổi tên|project name|tên dự án/i.test(labels(field))
      && !fields.some(parent => parent !== field && parent.contains(field)));
    const matches = eligible.filter(field => /prompt|what (?:do|would) you|describe|mô tả|bạn muốn|tạo nội dung/i.test(labels(field)));
    if (matches.length === 1) return matches[0];
    return eligible.length === 1 ? eligible[0] : null;
  }
  const ratioLabel = ratio => new RegExp("(?:^|[^0-9])" + ratio.replace(":", "\\s*[:∶]\\s*") + "(?![0-9])", "u");
  async function selectRatio(ratio) {
    const selected = node => ["aria-checked", "aria-selected", "aria-pressed"].some(key => node.getAttribute(key) === "true")
      || node.getAttribute("data-state") === "checked" || node.getAttribute("data-state") === "active";
    const controls = () => [...document.querySelectorAll("button,[role=button],[role=radio],[role=option],[role=menuitemradio],[role=tab]")]
      .filter(node => visible(node) && !node.disabled && node.getAttribute("aria-disabled") !== "true");
    const labels = node => [caption(node), node.getAttribute("data-value") || "", node.innerText || ""].join(" ");
    const exact = ratioLabel(ratio);
    const hasRatio = ratioLabel("[0-9]+:[0-9]+");
    const trigger = controls().filter(node => /aspect ratio|tỷ lệ|tỉ lệ|crop_(?:landscape|portrait)/iu.test(labels(node))
      || (node.getAttribute("aria-haspopup") && hasRatio.test(labels(node))));
    const options = () => controls().filter(node => !trigger.includes(node) && exact.test(labels(node)));
    const modelSettings = controls().filter(node => /Nano Banana|Imagen/iu.test(labels(node)) && /[x×][1-4]\b/iu.test(labels(node)));
    const settings = modelSettings.length === 1 ? modelSettings[0] : controls().find(node => /^(Điều kiện kích hoạt cài đặt|Settings trigger)$/iu.test(caption(node)));
    let opened = false;
    if (!options().length && settings) {
      settings.click();
      opened = true;
      const image = await waitFor(() => controls().find(node => node.getAttribute("role") === "radio" && /^(Image|Hình ảnh|Ảnh)$/iu.test(caption(node))), 3);
      if (image && !selected(image)) image.click();
    }
    if (!options().length) await waitFor(() => options().length, 3);
    if (!options().length && trigger.length === 1) trigger[0].click();
    const option = await waitFor(() => {
      if (trigger.length === 1 && exact.test(labels(trigger[0])) && !options().length) return trigger[0];
      const matches = options();
      return matches.length === 1 ? matches[0] : null;
    }, 5);
    if (!option) throw Error("Không tìm thấy tỷ lệ " + ratio + " trong Google Flow.");
    if (!trigger.includes(option) && !selected(option)) option.click();
    const single = controls().filter(node => /^[x×]\s*1$/iu.test(caption(node)));
    if (single.length === 1 && !selected(single[0])) single[0].click();
    if (opened && options().length && settings.isConnected) settings.click();
  }
  async function run() {
    let appliedRatio;
    const batch = await send({op:"batch"});
    if (batch.done) return;
    const queued = [];
    const results = [];
    const cards = () => [...document.querySelectorAll('[role="group"][aria-label]')];
    const normalize = text => (text || "").replace(/\s+/gu, " ").trim();
    for (const [offset, prompt] of batch.prompts.entries()) {
      const job = {job:batch.job, index:batch.index + offset, prompt};
      const agent = [...document.querySelectorAll('[role="checkbox"],input[type="checkbox"]')].find(node => visible(node)
        && /^(Tác nhân|Agent)$/iu.test((node.getAttribute("aria-label") || node.textContent || "").trim())
        && (node.checked || node.getAttribute("aria-checked") === "true"));
      if (agent) { agent.click(); await sleep(300); }
      let field = await waitFor(promptField, 25);
      if (!field) throw Error("Không xác định được ô prompt duy nhất. Mở trang tạo ảnh trong dự án Flow.");
      const ratio = /^Tạo một ảnh minh họa tỷ lệ (\d+:\d+) /u.exec(job.prompt)?.[1];
      if (ratio && ratio !== appliedRatio) {
        await selectRatio(ratio);
        appliedRatio = ratio;
        field = await waitFor(promptField, 10);
        if (!field) throw Error("Không thấy ô prompt sau khi chọn tỷ lệ.");
      }
      await send({op:"activate"});
      if (!await waitFor(() => document.hasFocus() && !document.hidden, 6))
        throw Error("Chrome chưa đưa tab Flow lên trước để gửi prompt.");
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
      const start = buttons.filter(button => /^(Bắt đầu tạo|Start generating|Start creating)$/iu.test(caption(button)));
      let generate = start.length === 1 ? start : buttons.filter(b=>/^(Generate Image|Create Image|Tạo hình ảnh|Tạo ảnh)$/i.test(caption(b)));
      if (generate.length === 0 && matching(/Nano Banana|Imagen/i).length === 1)
        generate = buttons.filter(b=>/^(Generate|Tạo)$/i.test(caption(b)));
      if (generate.length === 0) {
        const editor = field.closest("form") || field.parentElement;
        generate = buttons.filter(button => /^(arrow_forward|send|gửi|bắt đầu tạo|start generating|start creating)$/iu.test(caption(button))
          && (editor?.contains(button) || field.getBoundingClientRect().bottom <= button.getBoundingClientRect().bottom
            && Math.abs(field.getBoundingClientRect().right - button.getBoundingClientRect().right) < 100));
      }
      if (generate.length !== 1) throw Error("Không nhận diện được nút tạo ảnh. Kiểm tra chế độ Ảnh, model và hạn mức Flow.");
      const prior = new Set(cards());
      const editorBox = field.getBoundingClientRect();
      const nearbyArrows = buttons.filter(button => {
        const box = button.getBoundingClientRect();
        return box.top >= editorBox.bottom - 8 && box.top <= editorBox.bottom + 100
          && Math.abs(box.right - editorBox.right) < 100
          && ([...button.querySelectorAll("mat-icon,[data-icon],[aria-hidden=true],img")].some(icon =>
            /^(arrow_forward|send)$/i.test((icon.getAttribute("data-icon") || icon.getAttribute("alt") || icon.textContent || "").trim()))
            || /^(Bắt đầu tạo|Start generating|Start creating)$/iu.test(caption(button)));
      });
      const submit = nearbyArrows.length === 1 ? nearbyArrows[0] : generate[0];
      submit.scrollIntoView({block:"nearest", inline:"nearest"});
      const box = submit.getBoundingClientRect();
      const x = box.left + box.width / 2;
      const y = box.top + box.height / 2;
      if (!submit.contains(document.elementFromPoint(x,y))) throw Error("Nút tạo ảnh đang bị che.");
      await send({op:"submit", job:job.job, x, y});
      const findCard = () => {
        const matches = cards().filter(card => !prior.has(card) &&
          (normalize(card.getAttribute("aria-label")) === normalize(prompt)
           || normalize(card.textContent).includes(normalize(prompt))));
        return matches.length === 1 ? matches[0] : null;
      };
      const card = await waitFor(findCard, 20);
      if (!card) throw Error("Flow chưa xác nhận đã nhận prompt cảnh " + (job.index + 1) + ".");
      queued.push({job, card, findCard});
      await send({op:"progress", count:queued.length, stage:"submitted"});
    }
    const claimed = new Set();
    for (const {job, card, findCard} of queued) {
      const result = await waitFor(() => {
        const current = card.isConnected ? card : findCard();
        if (!current || claimed.has(current)) return null;
        const urls = [...new Set(candidates().filter(image => current.contains(image)).map(image => image.currentSrc))];
        if (urls.length !== 1) return null;
        claimed.add(current);
        return urls[0];
      }, 175);
      if (!result) throw Error("Không tìm thấy ảnh kết quả của cảnh " + (job.index + 1) + ".");
      results.push({job, result});
      await send({op:"progress", count:results.length});
    }
    for (const {job, result} of results) {
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
    if (pairing) history.replaceState(history.state, "", location.pathname + location.search); 
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
    
    await run();
  }
  connect().catch(async error => {
    try {await send({op:"error",message:error.message});} catch { console.warn("Vẽ Tay: ghép nối hoặc gửi trạng thái thất bại.", error.message); }
  }).finally(()=>{window.__veTayRunning=false;});
})();