/** @typedef {{id:number,name:string,description:string,original_sql:string}} Sample */
/** @typedef {{id:number,name:string,sample_id:number,candidate_sql:string,status:string}} Batch */
/** @typedef {{id:number,batch_id:number,result:Object}} Evidence */
/** @typedef {{id:number,batch_id:number,invoice_id:number,status:string,fail_reason:?string,created_at:string,updated_at:string}} Withdrawal */
export const entityNames={sample:'SQL 样本',batch:'复核批次',evidence:'执行证据',withdrawal:'撤下登记'};
