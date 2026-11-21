import{api,post}from'./client.js';
export const listWithdrawals=(params='')=>api('/withdrawals'+params);
export const eligibleWithdrawals=batchId=>api('/withdrawals/eligible/'+batchId);
export const createWithdrawals=(batch_id,invoice_ids)=>post('/withdrawals',{batch_id,invoice_ids});
export const retryWithdrawal=id=>post('/withdrawals/'+id+'/retry',{});
