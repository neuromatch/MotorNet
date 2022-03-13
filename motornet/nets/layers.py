import numpy as np
import tensorflow as tf
from tensorflow.keras.layers import Layer, GRUCell, Dense, Lambda


class GRUNetwork(Layer):
    def __init__(self, plant, n_units=20, n_hidden_layers=1, activation='tanh', kernel_regularizer=0.,
                 recurrent_regularizer=0., proprioceptive_noise_sd=0., visual_noise_sd=0.,
                 hidden_noise_sd=0., n_ministeps=1, output_bias_initializer=tf.initializers.Constant(value=-5),
                 output_kernel_initializer=tf.keras.initializers.random_normal(stddev=10 ** -3), **kwargs):

        if type(n_units) == int:
            n_units = list(np.repeat(n_units, n_hidden_layers).astype('int32'))
        if len(n_units) > 1 and n_hidden_layers == 1:
            n_hidden_layers = len(n_units)
        if len(n_units) != n_hidden_layers:
            raise ValueError('The number of hidden layers should match the size of the n_unit array.')

        # set noise levels
        self.proprioceptive_noise_sd = proprioceptive_noise_sd
        self.visual_noise_sd = visual_noise_sd
        self.hidden_noise_sd = hidden_noise_sd

        # plant states
        self.proprioceptive_delay = plant.proprioceptive_delay
        self.visual_delay = plant.visual_delay
        self.n_muscles = plant.n_muscles
        self.state_size = [tf.TensorShape([plant.output_dim]),
                           tf.TensorShape([plant.output_dim]),
                           tf.TensorShape([plant.muscle_state_dim, self.n_muscles]),
                           tf.TensorShape([plant.geometry_state_dim, self.n_muscles]),
                           tf.TensorShape([self.n_muscles * 2, self.proprioceptive_delay]),  # muscle length & velocity
                           tf.TensorShape([plant.space_dim, self.visual_delay])]
        # hidden states for GRU layer(s)
        for n in n_units:
            self.state_size.append(tf.TensorShape([n]))

        # create attributes
        self.n_ministeps = int(np.maximum(n_ministeps, 1))
        self.output_size = self.state_size
        self.plant = plant
        self.kernel_regularizer_weight = kernel_regularizer  # to save the values in `get_save_config`
        self.kernel_regularizer = tf.keras.regularizers.l2(kernel_regularizer)
        self.recurrent_regularizer_weight = recurrent_regularizer  # to save the values in `get_save_config`
        self.recurrent_regularizer = tf.keras.regularizers.l2(recurrent_regularizer)
        self.output_bias_initializer = output_bias_initializer
        self.output_kernel_initializer = output_kernel_initializer
        self.n_hidden_layers = n_hidden_layers
        if activation == 'recttanh':
            self.activation = recttanh
            self.activation_name = 'recttanh'
        else:
            self.activation = activation
            self.activation_name = activation
        self.n_units = n_units
        self.layers = []
        # functionality for recomputing inputs at every timestep
        self.do_recompute_inputs = False
        self.recompute_inputs = lambda inputs, states: inputs

        # create Lambda-wrapped functions (to prevent memory leaks)
        def get_new_proprio_feedback(mstate):
            # normalise by muscle characteristics
            muscle_len = tf.slice(mstate, [0, 1, 0], [-1, 1, -1]) / self.plant.muscle.l0_ce
            muscle_vel = tf.slice(mstate, [0, 2, 0], [-1, 1, -1]) / self.plant.muscle.vmax
            # flatten muscle length and velocity
            proprio_true = tf.reshape(tf.concat([muscle_len, muscle_vel], axis=1), shape=(-1, self.n_muscles * 2))
            return proprio_true

        def get_new_visual_feedback(cstate):
            visual_true, _ = tf.split(cstate, 2, axis=-1)  # position only (discard velocity)
            return visual_true

        self.unpack_plant_states = Lambda(lambda x: x[:4], name="unpack_plant_states")
        self.unpack_feedback_states = Lambda(lambda x: x[4:6], name="unpack_feedback_states")
        self.get_feedback_backlog = Lambda(lambda x: tf.slice(x, [0, 0, 1], [-1, -1, -1]), name="get_feedback_backlog")
        self.get_feedback_current = \
            Lambda(lambda x: tf.squeeze(tf.slice(x, [0, 0, 0], [-1, -1, 1]), axis=-1), name="get_feedback_current")
        self.lambda_cat = Lambda(lambda x: tf.concat(x, axis=-1), name="lambda_cat")
        self.lambda_cat2 = Lambda(lambda x: tf.concat(x, axis=2), name="lambda_cat2")
        self.add_noise = Lambda(lambda x: x[0] + tf.random.normal(tf.shape(x[0]), stddev=x[1]), name="add_noise")
        self.tile_feedback = Lambda(lambda x: tf.tile(x[0][:, :, tf.newaxis], [1, 1, x[1]]), name="tile_feedback")
        self.get_new_proprio_feedback = Lambda(lambda x: get_new_proprio_feedback(x), name="get_new_proprio_feedback")
        self.get_new_visual_feedback = Lambda(lambda x: get_new_visual_feedback(x), name="get_new_visual_feedback")
        self.get_new_hidden_state =\
            Lambda(lambda x: [tf.zeros((x[0], n_u), dtype=x[1]) for n_u in self.n_units], name="get_new_hidden_state")
        self.built = False

        output_names = [
            'joint position',
            'cartesian position',
            'muscle state',
            'geometry state',
            'proprioceptive feedback',
            'visual feedback']
        output_names.extend(['gru_hidden' + str(k) for k in range(self.n_hidden_layers)])
        self.output_names = output_names
        super().__init__(**kwargs)

    def build(self, input_shapes):
        for k in range(self.n_hidden_layers):
            layer = GRUCell(units=self.n_units[k],
                            activation=self.activation,
                            name='hidden_layer_' + str(k),
                            kernel_regularizer=self.kernel_regularizer,
                            recurrent_regularizer=self.recurrent_regularizer,
                            )
            self.layers.append(layer)
        output_layer = Dense(units=self.plant.input_dim,
                             activation='sigmoid',
                             name='output_layer',
                             bias_initializer=self.output_bias_initializer,
                             kernel_initializer=self.output_kernel_initializer,
                             kernel_regularizer=self.kernel_regularizer,
                             )
        self.layers.append(output_layer)
        self.built = True

    def get_base_config(self):
        cfg = {'proprioceptive_noise_sd': self.proprioceptive_noise_sd, 'visual_noise_sd': self.visual_noise_sd,
               'hidden_noise_sd': self.hidden_noise_sd, 'proprioceptive_delay': self.proprioceptive_delay,
               'visual_delay': self.visual_delay, 'n_muscle': self.n_muscles,
               'n_ministeps': self.n_ministeps,
               'kernel_regularizer_weight': self.kernel_regularizer_weight,
               'recurrent_regularizer_weight': self.recurrent_regularizer_weight, 'n_units': int(self.n_units[0]),
               'n_hidden_layers': self.n_hidden_layers, 'activation': self.activation_name}
        return cfg

    def get_save_config(self):
        return self.get_base_config()

    @classmethod
    def from_config(cls, config):
        return cls(**config)

    def call(self, inputs, states=None, **kwargs):
        # unpack states
        new_hidden_states_dict = {}
        new_hidden_states = []

        # handle feedback
        old_proprio_feedback, old_visual_feedback = self.unpack_feedback_states(states)
        proprio_backlog = self.get_feedback_backlog(old_proprio_feedback)
        visual_backlog = self.get_feedback_backlog(old_visual_feedback)
        proprio_fb = self.get_feedback_current(old_proprio_feedback)
        visual_fb = self.get_feedback_current(old_visual_feedback)

        # if the task demands it, inputs will be recomputed at every timestep
        if self.do_recompute_inputs:
            inputs = self.recompute_inputs(inputs, states)

        x = self.lambda_cat((proprio_fb, visual_fb, inputs.pop("inputs")))

        # net forward pass
        for k in range(self.n_hidden_layers):
            x, new_hidden_state = self.layers[k](x, states[6+k])
            new_hidden_state_noisy = self.add_noise((new_hidden_state, self.hidden_noise_sd))
            new_hidden_states_dict['gru_hidden' + str(k)] = new_hidden_state_noisy
            new_hidden_states.append(new_hidden_state_noisy)
        u = self.layers[-1](x)

        # plant forward pass
        jstate, cstate, mstate, gstate = self.unpack_plant_states(states)
        for _ in range(self.n_ministeps):
            jstate, cstate, mstate, gstate = self.plant(u, jstate, mstate, gstate, **inputs)

        proprio_true = self.get_new_proprio_feedback(mstate)
        visual_true = self.get_new_visual_feedback(cstate)
        proprio_noisy = self.add_noise((proprio_true, self.proprioceptive_noise_sd))
        visual_noisy = self.add_noise((visual_true, self.visual_noise_sd))
        new_proprio_feedback = self.lambda_cat2((proprio_backlog, proprio_noisy[:, :, tf.newaxis]))
        new_visual_feedback = self.lambda_cat2((visual_backlog, visual_noisy[:, :, tf.newaxis]))

        # pack new states
        new_states = [jstate, cstate, mstate, gstate, new_proprio_feedback, new_visual_feedback]
        new_states.extend(new_hidden_states)

        # pack output
        output = {'joint position': jstate,
                  'cartesian position': cstate,
                  'muscle state': mstate,
                  'geometry state': gstate,
                  'proprioceptive feedback': new_proprio_feedback,
                  'visual feedback': new_visual_feedback,
                  **new_hidden_states_dict}

        return output, new_states

    def get_initial_state(self, inputs=None, batch_size=1, dtype=tf.float32):
        if inputs is not None:
            states = self.plant.get_initial_state(joint_state=inputs, batch_size=batch_size)
        else:
            states = self.plant.get_initial_state(batch_size=batch_size)
        hidden_states = self.get_new_hidden_state((batch_size, dtype))

        proprio_true = self.get_new_proprio_feedback(states[2])
        visual_true = self.get_new_visual_feedback(states[1])
        proprio_tiled = self.tile_feedback((proprio_true, self.proprioceptive_delay))
        visual_tiled = self.tile_feedback((visual_true, self.visual_delay))
        proprio_noisy = self.add_noise((proprio_tiled, self.proprioceptive_noise_sd))
        visual_noisy = self.add_noise((visual_tiled, self.visual_noise_sd))

        states.append(proprio_noisy)
        states.append(visual_noisy)
        states.extend(hidden_states)
        return states


# Custom activation function (rectified hyperbolic tangent)
@tf.function
def recttanh(x):
    x = tf.keras.activations.tanh(x)
    x = tf.where(tf.less_equal(x, tf.constant(0.)), tf.constant(0.), x)
    return x
