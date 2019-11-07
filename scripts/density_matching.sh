# Load defaults for all experiments
source ./scripts/experiment_config_density.sh

# activate virtual environment
source ./venv/bin/activate

# variables specific to this experiment
num_steps=100001
exp_name=density_matching
logging=5000
iters_per_component=20000
min_beta=1.0
regularization_rate=0.75
plot_resolution=500


for u in 2 3 4 1 0
do

    for flow_depth in 8 16
    do

        # boosted model
        python density.py --dataset u${u} \
               --experiment_name ${exp_name} \
               --no_cuda \
               --num_workers ${num_workers} \
               --plot_resolution ${plot_resolution} \
               --num_steps ${num_steps} \
               --min_beta ${min_beta} \
               --iters_per_component ${iters_per_component} \
               --flow boosted \
               --component_type ${component_type} \
               --num_components 2 \
               --regularization_rate ${regularization_rate} \
               --num_flows ${flow_depth} \
               --z_size ${z_size} \
               --batch_size ${batch_size} \
               --manual_seed ${seed} \
               --log_interval ${logging} \
               --plot_interval ${logging} &

        # planar flow
        python density.py --dataset u${u} \
               --experiment_name ${exp_name} \
               --no_cuda \
               --num_steps ${num_steps} \
               --plot_resolution ${plot_resolution} \
               --min_beta ${min_beta} \
               --iters_per_component ${iters_per_component} \
               --num_workers ${num_workers} \
               --flow ${component_type} \
               --num_flows ${flow_depth} \
               --z_size ${z_size} \
               --batch_size ${batch_size} \
               --manual_seed ${seed} \
               --log_interval ${logging} \
               --plot_interval ${logging} ;


    done

done


    
