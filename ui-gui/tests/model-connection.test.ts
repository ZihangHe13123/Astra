import {test} from 'node:test';
import assert from 'node:assert/strict';
import {modelConnection} from '../src/renderer/model-connection.js';

test('unconfigured model routes to its exact endpoint, preserving billing and custom URLs',()=>{
 const info:any={providers:[{id:'vendor',connected:false}],connection_routes:[
  {id:'vendor',base_url:'https://api.example/v1'},
  {id:'plan',base_url:'https://plan.example/v1'},
  {id:'custom',base_url:''}]};
 const plan=modelConnection(info,{type:'model',provider_id:'vendor',endpoint:'https://plan.example/v1/'});
 assert.equal(plan.configured,false);assert.equal(plan.route?.id,'plan');
 assert.equal(modelConnection(info,{type:'model',provider_id:'vendor',endpoint:'https://private.example/v1'}).route?.id,'custom');
 assert.equal(modelConnection(info).configured,undefined);
});
test('a configured provider stays usable while another provider is missing credentials',()=>{
 const info:any={providers:[{id:'ready',connected:true},{id:'missing',connected:false}],connection_routes:[]};
 assert.equal(modelConnection(info,{type:'model',provider_id:'ready'}).configured,true);
 assert.equal(modelConnection(info,{type:'model',provider_id:'missing'}).configured,false);
});
