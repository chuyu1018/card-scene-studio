// Exercise the editor's actual geometry without browser automation or duplicate algorithms.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../assets/editor/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script); // Entire shipped JavaScript must parse.
const context = vm.createContext({assert});
vm.runInContext(script.slice(0, script.indexOf("\ndocument.querySelectorAll('[data-mode]')")), context);
vm.runInContext(`
for (const preset of Object.keys(PRESETS)) {
  for (let n=1;n<=12;n++) {
    const [width,height,shape]=PRESETS[preset];
    state.mode='multi';state.canvas={width,height,shape,preset};
    state.cards=Array.from({length:n},(_,i)=>({id:String(i),ratio:i%3===0?733/1024:i%3===1?1.5:.6}));
    arrangeMultiCards();
    for(const c of state.cards) {
      assert.ok(c.width>0&&c.height>0);
      assert.ok(Math.max(c.width,c.height)<=650.001);
      assert.ok(c.x>=-1e-5&&c.y>=-1e-5&&c.x+c.width<=width+1e-5&&c.y+c.height<=height+1e-5);
      const cx=c.x+c.width/2,cy=c.y+c.height/2;
      assert.ok(constraints(c.width,c.height).every(k=>k.a*cx+k.b*cy>=k.c-1e-5));
    }
    for(let i=0;i<n;i++)for(let j=i+1;j<n;j++) {
      const a=state.cards[i],b=state.cards[j];
      assert.ok(a.x+a.width<=b.x+1e-5||b.x+b.width<=a.x+1e-5||a.y+a.height<=b.y+1e-5||b.y+b.height<=a.y+1e-5, preset+' overlap '+n);
    }
  }
}
state.mode='multi';state.canvas={width:1350,height:2400,shape:'rectangle',preset:'9:16'};
state.cards=[{ratio:733/1024},{ratio:733/1024}];arrangeMultiCards();
assert.equal(state.cards[0].height,650);assert.notEqual(state.cards[0].x,state.cards[1].x);assert.notEqual(state.cards[0].y,state.cards[1].y);
state.mode='grid';state.cards=[{ratio:733/1024},{ratio:733/1024},{ratio:733/1024}];assignGridSlots();
assert.equal(state.cards.map(c=>c.slot).join(','),'6,4,2');
const prompt=generationPrompt({mode:'grid',canvas:state.canvas,cards:state.cards.map(c=>({...c,name:'demo',rotation:0}))},'layout.json');
assert.ok(prompt.includes('附上'));assert.ok(prompt.includes('第 7 格'));assert.ok(!prompt.includes('Codex'));
`, context);
console.log('Passed: defaults, 84 packing cases, grid slots, portable prompt, full-script syntax.');
