(() => {
  const pairing = /^#vetay=([a-f0-9]{32})(?:-([0-9]{4,5}))?$/.exec(location.hash);
  if (window.__veTayRunning) return;
  window.__veTayRunning = true;
  const sleep = (ms) => new Promise(resolve => setTimeout(resolve,ms));
  const visible = node => node.getClientRects().length && getComputedStyle(node).visibility !== "hidden";
  const send = async (message) => {
    const response = await chrome.runtime.sendMessage(message);
    if (!response?.ok) throw Error(response?.error || "Mất kết nối với Vẽ Tay.");
    return response.result;
  };
  function resultImages(card) {
    return [...card.querySelectorAll("img")].filter(img => {
      if (!img.complete || !img.naturalWidth || !img.naturalHeight || !visible(img)) return false;
      const src = img.currentSrc || img.src;
      const url = new URL(src, location.href);
      const media = /^(Ô hiển thị hình ảnh của người dùng|User image tile)$/iu.test(img.alt.trim());
      const original = /^(blob:|https:\/\/[^/]*\.googleusercontent\.com\/)/i.test(src);
      return original || (media && ["https:", "data:", "blob:"].includes(url.protocol));
    });
  }
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
    const iconName = "crop_" + ratio.replace(":", "_");
    const matchesRatio = node => exact.test(labels(node)) || [...node.querySelectorAll("mat-icon,[role=img],svg")]
      .some(icon => [icon.textContent, icon.getAttribute("aria-label"), icon.getAttribute("data-icon")]
        .some(label => label?.trim() === iconName));
    const hasRatio = ratioLabel("[0-9]+:[0-9]+");
    const isOption = node => ["radio", "option", "menuitemradio"].includes(node.getAttribute("role"));
    const trigger = controls().filter(node => !isOption(node) && (node.getAttribute("aria-haspopup")
      ? /aspect ratio|tỷ lệ|tỉ lệ/iu.test(labels(node)) || hasRatio.test(labels(node))
      : /^(?:aspect ratio|tỷ lệ(?: khung hình)?|tỉ lệ(?: khung hình)?)$/iu.test(caption(node))));
    const options = () => controls().filter(node => !trigger.includes(node) && node !== settings && matchesRatio(node));
    const modelSettings = controls().filter(node => /Nano Banana|Imagen/iu.test(labels(node)) && /[x×][1-4]\b/iu.test(labels(node)));
    const settings = controls().find(node => /^(Điều kiện kích hoạt cài đặt|Settings trigger|Generation settings)$/iu.test(caption(node)))
      || (modelSettings.length === 1 ? modelSettings[0] : null);
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
      const radios = matches.filter(isOption);
      return radios.length === 1 ? radios[0] : matches.length === 1 ? matches[0] : null;
    }, 12);
    if (!option) throw Error("Không chọn được tỷ lệ " + ratio + " trong Flow (" + (settings ? "đã thấy nút cài đặt ảnh" : "chưa thấy nút cài đặt ảnh") + ").");
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
    const initialCards = cards();
    let newestFirst = true;
    for (const [offset, prompt] of batch.prompts.entries()) {
      const job = {job:batch.job, index:batch.index + offset, prompt};
      const agent = [...document.querySelectorAll('[role="checkbox"],input[type="checkbox"]')].find(node => visible(node)
        && /^(Tác nhân|Agent)$/iu.test((node.getAttribute("aria-label") || node.textContent || "").trim())
        && (node.checked || node.getAttribute("aria-checked") === "true"));
      if (agent) { agent.click(); await sleep(300); }
      let field = await waitFor(promptField, 25);
      if (!field) throw Error("Không xác định được ô prompt duy nhất. Mở trang tạo ảnh trong dự án Flow.");
      await send({op:"activate"});
      const ratio = /^Tạo một ảnh minh họa tỷ lệ (\d+:\d+) /u.exec(job.prompt)?.[1];
      if (ratio && ratio !== appliedRatio) {
        await selectRatio(ratio);
        appliedRatio = ratio;
        field = await waitFor(promptField, 10);
        if (!field) throw Error("Không thấy ô prompt sau khi chọn tỷ lệ.");
      }
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
      const currentCards = cards();
      newestFirst &&= currentCards.length === initialCards.length + queued.length + 1
        && currentCards[0] === card;
      queued.push({job, card, findCard});
      await send({op:"progress", count:queued.length, stage:"submitted"});
    }
    const claimed = new Set();
    for (const {job, card, findCard} of queued) {
      const result = await waitFor(() => {
        const grid = cards();
        const replacement = newestFirst && initialCards.length === 0 && grid.length === queued.length
          && grid.every(item => resultImages(item).length === 1)
          ? grid[queued.length - 1 - queued.findIndex(entry => entry.card === card)] : null;
        const current = (card.isConnected && resultImages(card).length ? card : findCard()) || replacement;
        if (!current || claimed.has(current)) return null;
        const urls = [...new Set(resultImages(current).map(image => image.currentSrc || image.src))];
        if (urls.length !== 1) return null;
        claimed.add(current);
        return current;
      }, 175);
      if (!result) throw Error("Không tìm thấy ảnh kết quả của cảnh " + (job.index + 1) + ".");
      results.push({job, result});
      await send({op:"progress", count:results.length});
    }
    for (const {job, result:card} of results) {
      if (!card.isConnected) throw Error("Thẻ ảnh cảnh " + (job.index + 1) + " đã thay đổi.");
      const activateControl = async (node, clicks=1) => {
        node.scrollIntoView({block:"center", inline:"nearest"});
        const box = node.getBoundingClientRect();
        const x = box.left + box.width / 2, y = box.top + box.height / 2;
        if (!node.contains(document.elementFromPoint(x,y))) throw Error("Nút tải ảnh Flow đang bị che.");
        await send({op:"submit", job:job.job, x, y, clicks});
      };
      const tile = resultImages(card)[0];
      await activateControl(tile, 2);
      const download = await waitFor(() => matching(/^(Tải nội dung nghe nhìn xuống|Download media|Download)$/iu)[0], 12);
      if (!download) throw Error("Không thấy nút tải ảnh gốc của cảnh " + (job.index + 1) + ".");
      await activateControl(download);
      const option = await waitFor(() => [...document.querySelectorAll('[role="menuitem"],button')]
        .find(item => visible(item) && /^2K(?:\s|$)/iu.test(caption(item)) && !item.disabled && item.getAttribute("aria-disabled") !== "true"), 8);
      if (!option) throw Error("Flow không có tùy chọn tải 2K cho cảnh " + (job.index + 1) + ".");
      await send({op:"arm2k",job:job.job,index:job.index});
      await activateControl(option);
      const deadline = Date.now() + 150000;
      let received = false;
      while (!received && Date.now() < deadline) {
        received = (await send({op:"transfer2k",job:job.job,index:job.index})).received;
        if (!received) await sleep(1000);
      }
      if (!received) throw Error("Chrome chưa tải xong ảnh 2K cho cảnh " + (job.index + 1) + ".");
      if (job.index !== batch.index + batch.prompts.length - 1) {
        const back = await waitFor(() => matching(/^(Nút quay lại để quay về trang trước|Go back|Back)$/iu)[0], 8);
        if (!back) throw Error("Không thấy nút trở lại lưới ảnh Flow.");
        await activateControl(back);
        await waitFor(() => cards().length > 0, 12);
        for (const entry of results) {
          if (entry.result.isConnected) continue;
          const replacement = newestFirst && initialCards.length === 0 && cards().length === queued.length
            ? cards()[queued.length - 1 - (entry.job.index - batch.index)] : null;
          if (replacement) entry.result = replacement;
        }
      }
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
    const state = await send(pairing ? {op:"pair",token:pairing[1],port:Number(pairing[2] || 7877)} : {op:"resume"});
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