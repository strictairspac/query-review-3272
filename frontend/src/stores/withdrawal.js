import{listWithdrawals}from'../api/withdrawal.js';
export const withdrawals={
  items:[],
  async load(params=''){this.items=await listWithdrawals(params);return this.items}
};
