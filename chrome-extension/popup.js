const code = document.getElementById("code");
const status = document.getElementById("status");
chrome.storage.session.get("pairing").then(({pairing}) => { if (pairing) code.value = pairing; });
document.getElementById("run").onclick = async () => {
  try {
    const token = code.value.trim();
    if (!/^[a-f0-9]{32}$/.test(token)) throw Error("Dán mã kết nối từ Vẽ Tay.");
    const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
    if (!/^https:\/\/(flow\.google\.com|labs\.google)\//.test(tab?.url || "")) throw Error("Mở tab dự án Google Flow.");
    const result = await chrome.runtime.sendMessage({op:"begin", token, tabId:tab.id});
    if (!result.ok) throw Error(result.error);
    status.textContent = "Đã bắt đầu. Giữ tab Flow mở để tiện ích xử lý.";
  } catch(error) { status.textContent = error.message; }
};