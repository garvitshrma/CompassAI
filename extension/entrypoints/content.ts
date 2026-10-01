export default defineContentScript({
  matches: ['<all_urls>'],
  main(){
    browser.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (message.type === 'GET_PAGE') {
        sendResponse(readPage());
      }
    })
  }
})


function readPage(){
  const headings = [...document.querySelectorAll<HTMLElement>('h1 ,h2 , h3')].map((h) => h.innerText)
  return {title: document.title , headings: headings}
}
