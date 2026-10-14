"""Create known-answer scanned PDF/image fixtures with no text layer."""
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parents[1]
output=root/"artifacts/ocr"
output.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=["--no-sandbox"])
    page=browser.new_page(viewport={"width":1100,"height":1200},device_scale_factor=1)
    page.set_content('''<!doctype html><html lang="zh-CN"><body><h1>高等数学识别验证</h1>
        <p>这是扫描资料中的中文正文。公式应保留上下标、分数、积分上下限和绝对值。</p>
        <h2>一、求导与积分</h2><div id="a"></div><div id="b"></div>
        <h2>二、极限与定义域</h2><div id="c"></div><div id="d"></div>
        <p>注意：实数 x 不一定为正，使用公式时必须检查适用条件。</p>
        <h2>三、表格</h2><table><tr><th>知识点</th><th>题目数量</th></tr>
        <tr><td>导数</td><td>5</td></tr><tr><td>积分</td><td>8</td></tr></table></body></html>''')
    page.add_style_tag(path=str(root/"frontend/node_modules/katex/dist/katex.min.css"))
    page.add_style_tag(content='''body{font-family:"Droid Sans Fallback",sans-serif;margin:65px;font-size:22px;line-height:1.7;color:black;background:white}
        h1{font-size:36px}h2{font-size:27px;margin-top:35px}table{border-collapse:collapse;width:70%}
        th,td{border:2px solid black;padding:9px}.katex{font-size:1.5em}''')
    page.add_script_tag(path=str(root/"frontend/node_modules/katex/dist/katex.min.js"))
    page.evaluate(r'''() => {const eq={a:String.raw`\frac{d}{dx}x^3=3x^2`,b:String.raw`\int_0^1 x^2\,dx=\frac{1}{3}`,
        c:String.raw`\lim_{x\to 0}\frac{\sin x}{x}=1`,d:String.raw`\sqrt{x^2}=|x|`};
        for(const [id,tex] of Object.entries(eq))katex.render(tex,document.getElementById(id),{displayMode:true});}''')
    page.screenshot(path=str(output/"scan-fixture.png"),full_page=True)
    page.locator('#b').screenshot(path=str(output/"formula-fixture.png"))
    browser.close()
Image.open(output/"scan-fixture.png").convert("RGB").save(output/"scan-fixture.pdf",resolution=140)
print("扫描 PDF、中文与公式图片样本已生成。PDF 只有图像，没有文字层。")
